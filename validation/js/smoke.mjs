// Reproducible end-to-end smoke test for resolve.mjs.
//
// Builds a tiny extracted-EPUB fixture in a temp dir, writes a jobs.json with a
// hand-computed range CFI plus two deliberately broken jobs, runs resolve.mjs,
// and asserts the results. Exits non-zero on any failure.

import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const HERE = dirname(fileURLToPath(import.meta.url));

const CONTAINER = `<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
`;

const OPF = `<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Smoke Book</dc:title>
    <dc:identifier id="bookid">smoke-1</dc:identifier>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>
`;

// p1 has three text-node children of interest:
//   T1 = "Before " , <em>mid</em> , T2 = " after 🐦 tail."
const CH1 = `<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 1</title></head>
<body>
<p id="p1">Before <em>mid</em> after 🐦 tail.</p>
<p id="p2">Second paragraph here.</p>
</body>
</html>
`;

const CH2 = `<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 2</title></head>
<body>
<p id="q1">Another <strong>chapter</strong> entirely.</p>
</body>
</html>
`;

function buildFixture(root) {
  mkdirSync(join(root, 'META-INF'), { recursive: true });
  mkdirSync(join(root, 'OEBPS'), { recursive: true });
  writeFileSync(join(root, 'META-INF', 'container.xml'), CONTAINER);
  writeFileSync(join(root, 'OEBPS', 'content.opf'), OPF);
  writeFileSync(join(root, 'OEBPS', 'chapter1.xhtml'), CH1);
  writeFileSync(join(root, 'OEBPS', 'chapter2.xhtml'), CH2);
}

// Hand-computed expected text for job (a): range from T1 offset 0 across <em>
// to T2 offset 9. T2 = " after 🐦 tail."; offset 9 is just past the emoji's
// low surrogate (🐦 = U+1F426 = 2 UTF-16 code units at indices 7,8).
//   T1.slice(0) + "mid" + T2.slice(0, 9)
//   = "Before " + "mid" + " after 🐦"
const EXPECTED_A = 'Before mid after \u{1F426}';

function main() {
  const tmp = mkdtempSync(join(tmpdir(), 'xpoint-smoke-'));
  const root = join(tmp, 'smoke-epub');
  buildFixture(root);

  const jobs = {
    books: [
      {
        id: 'smoke',
        root,
        jobs: [
          { index: 0, cfi: 'epubcfi(/6/2!/4/2,/1:0,/3:9)' }, // (a) valid range crossing <em> and the emoji
          { index: 1, cfi: 'epubcfi(/6/2!/4/2,/1:0,/3:9999)' }, // (b) offset far beyond text length
          { index: 2, cfi: 'epubcfi(/6/2!/4/2,/1:0,/3:9' }, // (c) syntactically invalid (missing paren)
        ],
      },
    ],
  };

  const jobsPath = join(tmp, 'jobs.json');
  const resultsPath = join(tmp, 'results.json');
  writeFileSync(jobsPath, JSON.stringify(jobs, null, 2));

  const proc = spawnSync(process.execPath, [join(HERE, 'resolve.mjs'), jobsPath, resultsPath], {
    encoding: 'utf8',
  });
  if (proc.status !== 0) {
    console.error('resolve.mjs exited non-zero');
    console.error(proc.stdout, proc.stderr);
    process.exit(1);
  }

  const out = JSON.parse(readFileSync(resultsPath, 'utf8'));
  const results = out.books[0].results;

  const failures = [];
  const check = (cond, msg) => {
    if (!cond) failures.push(msg);
  };

  const a = results.find((r) => r.index === 0);
  const b = results.find((r) => r.index === 1);
  const c = results.find((r) => r.index === 2);

  check(a && a.status === 'ok', `job (a) status: expected ok, got ${a && a.status} (${a && a.error})`);
  check(
    a && a.text === EXPECTED_A,
    `job (a) text mismatch:\n  expected: ${JSON.stringify(EXPECTED_A)}\n  actual:   ${JSON.stringify(a && a.text)}`,
  );
  check(b && b.status === 'error', `job (b) status: expected error, got ${b && b.status}`);
  check(c && c.status === 'error', `job (c) status: expected error, got ${c && c.status}`);

  console.log('results.json:');
  console.log(JSON.stringify(out, null, 2));

  if (failures.length) {
    console.error('\nSMOKE FAILED:');
    for (const f of failures) console.error('  - ' + f);
    rmSync(tmp, { recursive: true, force: true });
    process.exit(1);
  }

  console.log(`\nSMOKE OK — job (a) text === ${JSON.stringify(EXPECTED_A)}`);
  rmSync(tmp, { recursive: true, force: true });
}

main();
