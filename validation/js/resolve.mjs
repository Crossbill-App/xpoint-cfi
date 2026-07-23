// Independent CFI resolver for the xpoint-cfi validation pipeline.
//
// Reads a jobs.json (path = 1st CLI arg), resolves each range CFI against the
// extracted EPUB directory with `epub-cfi-resolver` + jsdom, extracts the text
// the CFI denotes, and writes results.json (path = 2nd CLI arg).
//
// Contract: see validation/VALIDATION.md "JS contract". Every job yields a
// result row; a single job's failure never aborts the run.

import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve as resolvePath } from 'node:path';
import { createRequire } from 'node:module';
import { JSDOM } from 'jsdom';

const require = createRequire(import.meta.url);
const CFI = require('epub-cfi-resolver');

const XHTML = 'application/xhtml+xml';
const HTML = 'text/html';

// Parse a file into a jsdom Document. Prefer strict XHTML/XML parsing so node
// counting and offsets match what the reading engine sees; fall back to the
// lenient HTML parser when the XML parser reports an error.
function parseDoc(filePath) {
  const src = readFileSync(filePath, 'utf8');
  try {
    const dom = new JSDOM(src, { contentType: XHTML });
    if (!dom.window.document.querySelector('parsererror')) {
      return dom.window.document;
    }
  } catch {
    // fall through to HTML
  }
  return new JSDOM(src, { contentType: HTML }).window.document;
}

// Resolve the OPF (package document) path from META-INF/container.xml.
function findOpfPath(root) {
  const containerPath = resolvePath(root, 'META-INF', 'container.xml');
  const doc = parseDoc(containerPath);
  const rootfile = doc.querySelector('rootfile');
  const fullPath = rootfile && rootfile.getAttribute('full-path');
  if (!fullPath) {
    throw new Error('container.xml has no rootfile full-path');
  }
  return resolvePath(root, fullPath);
}

// Build the entry document + fetch callback the resolver needs. The resolver
// hands raw hrefs (relative to the current document) to fetchCB; we track the
// current directory and resolve/cache against `root`. Documents are cached per
// absolute path.
function makeResolverContext(root) {
  const opfPath = findOpfPath(root);
  const cache = new Map();

  function load(absPath) {
    let doc = cache.get(absPath);
    if (!doc) {
      doc = parseDoc(absPath);
      cache.set(absPath, doc);
    }
    return doc;
  }

  const opfDir = dirname(opfPath);
  let currentDir = opfDir;
  const opfDoc = load(opfPath);

  const fetchCB = async (href) => {
    const absPath = resolvePath(currentDir, href);
    currentDir = dirname(absPath);
    return load(absPath);
  };

  // Spine hrefs are OPF-relative; currentDir only drifts for nested indirection
  // (iframe/embed), which one job must not leak into the next.
  const reset = () => {
    currentDir = opfDir;
  };

  return { opfDoc, fetchCB, reset };
}

// Ordered list of every text node in the document (document order).
function textNodesOf(doc) {
  const walker = doc.createTreeWalker(
    doc.documentElement,
    doc.defaultView.NodeFilter.SHOW_TEXT,
  );
  const nodes = [];
  let n = walker.nextNode();
  while (n) {
    nodes.push(n);
    n = walker.nextNode();
  }
  return nodes;
}

function isTextNode(node) {
  return node && node.nodeType === node.TEXT_NODE;
}

// Normalize a resolved boundary {node, offset?, relativeToNode?} to a concrete
// (index-into-textNodes, offset) pair. `isStart` selects the descendant/adjacent
// text node to use when the boundary landed on an element.
function boundaryToTextPos(loc, textNodes, isStart) {
  const offset = loc.offset || 0;
  const node = loc.node;

  if (isTextNode(node)) {
    const idx = textNodes.indexOf(node);
    if (idx < 0) return null;
    if (loc.relativeToNode === 'before') return { idx, offset: 0 };
    if (loc.relativeToNode === 'after') return { idx, offset: node.data.length };
    return { idx, offset };
  }

  // Element boundary: start -> before its first descendant text node,
  // end -> after its last descendant text node. If the element has no text
  // descendants, fall back to the nearest text node in document order.
  const FOLLOWING = node.DOCUMENT_POSITION_FOLLOWING;
  if (isStart) {
    for (let i = 0; i < textNodes.length; i++) {
      const t = textNodes[i];
      if (node.contains(t)) return { idx: i, offset: 0 };
      if (node.compareDocumentPosition(t) & FOLLOWING) return { idx: i, offset: 0 };
    }
    return null;
  }
  for (let i = textNodes.length - 1; i >= 0; i--) {
    const t = textNodes[i];
    if (node.contains(t)) return { idx: i, offset: t.data.length };
    const PRECEDING = node.DOCUMENT_POSITION_PRECEDING;
    if (node.compareDocumentPosition(t) & PRECEDING) return { idx: i, offset: t.data.length };
  }
  return null;
}

// Extract the text between two resolved boundaries in one document. Offsets are
// UTF-16 code units, which is exactly how JS strings index, so we slice directly.
function extractRangeText(from, to) {
  const doc = from.node.ownerDocument;
  const textNodes = textNodesOf(doc);

  const start = boundaryToTextPos(from, textNodes, true);
  const end = boundaryToTextPos(to, textNodes, false);
  if (!start || !end) {
    throw new Error('could not map CFI boundary to a text node');
  }

  if (start.idx > end.idx || (start.idx === end.idx && start.offset > end.offset)) {
    throw new Error('range start is after range end');
  }

  if (start.idx === end.idx) {
    return textNodes[start.idx].data.slice(start.offset, end.offset);
  }

  let out = textNodes[start.idx].data.slice(start.offset);
  for (let i = start.idx + 1; i < end.idx; i++) {
    out += textNodes[i].data;
  }
  out += textNodes[end.idx].data.slice(0, end.offset);
  return out;
}

async function resolveRange(ctx, cfiStr) {
  ctx.reset();
  const cfi = new CFI(cfiStr); // throws on invalid syntax
  if (!cfi.isRange) {
    return { error: 'not a range CFI' };
  }
  const { from, to } = await cfi.resolve(ctx.opfDoc, ctx.fetchCB);
  return { from, to };
}

async function runJob(ctx, cfiStr) {
  let { from, to, error } = await resolveRange(ctx, cfiStr);
  if (error) return { status: 'error', error };

  // epub-cfi-resolver bug: an end offset exactly equal to the text node's UTF-16
  // length (valid per spec: "after the last character") makes it walk past the node
  // list and return a node-less location. Retry with the final offset decremented,
  // then extend the extracted end by the one character we removed.
  let endExtend = 0;
  const endOffsetMatch = /:(\d+)(\])?\)$/.exec(cfiStr);
  if (to && !to.node && endOffsetMatch && Number(endOffsetMatch[1]) > 0) {
    const decremented =
      cfiStr.slice(0, endOffsetMatch.index) +
      `:${Number(endOffsetMatch[1]) - 1}` +
      (endOffsetMatch[2] || '') +
      ')';
    const retry = await resolveRange(ctx, decremented);
    if (!retry.error && retry.to && retry.to.node) {
      ({ from, to } = retry);
      endExtend = 1;
    }
  }

  if (!from || !from.node || !to || !to.node) {
    return { status: 'error', error: 'CFI did not resolve to nodes' };
  }
  if (endExtend) {
    to = { ...to, offset: (to.offset || 0) + endExtend };
  }

  if (from.node.ownerDocument !== to.node.ownerDocument) {
    return { status: 'cross-document' };
  }

  const text = extractRangeText(from, to);
  return { status: 'ok', text };
}

async function runBook(book) {
  const results = [];
  let ctx = null;
  let ctxError = null;
  try {
    ctx = makeResolverContext(book.root);
  } catch (err) {
    ctxError = err;
  }

  for (const job of book.jobs) {
    if (ctxError) {
      results.push({ index: job.index, status: 'error', error: String(ctxError.message || ctxError) });
      continue;
    }
    try {
      const r = await runJob(ctx, job.cfi);
      results.push({ index: job.index, ...r });
    } catch (err) {
      results.push({ index: job.index, status: 'error', error: String(err.message || err) });
    }
  }
  return { id: book.id, results };
}

async function main() {
  const jobsPath = process.argv[2];
  const resultsPath = process.argv[3];
  if (!jobsPath || !resultsPath) {
    console.error('usage: node resolve.mjs <jobs.json> <results.json>');
    process.exit(2);
  }

  const input = JSON.parse(readFileSync(jobsPath, 'utf8'));
  const books = [];
  for (const book of input.books || []) {
    books.push(await runBook(book));
  }

  writeFileSync(resultsPath, JSON.stringify({ books }, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
