# PowerShell SOPs — CCP corpus operations

Windows PowerShell 5.1, VS Code terminal, `D:\GovtExamAgent\ccp-mainbuild-v1`.
Everything here cost time at least once.

---

## Session setup

Environment variables are per-terminal. A new terminal has none of them.

```powershell
$env:CCP_API_BASE  = "https://ccp-api-demo.onrender.com"
$env:CCP_ADMIN_JWT = "<fresh token>"
$hdr  = @{ Authorization = "Bearer $env:CCP_ADMIN_JWT" }
$hdr2 = @{ Authorization = "Bearer $env:CCP_ADMIN_JWT"; "Content-Type" = "application/json" }
$PG   = 'postgresql://postgres:PASSWORD@db.ylfnbxyqiyiqvxtthhum.supabase.co:5432/postgres'
$env:PGCLIENTENCODING = "UTF8"
```

`CCP_API_BASE` is worth persisting; the JWT is not.

```powershell
[Environment]::SetEnvironmentVariable("CCP_API_BASE", "https://ccp-api-demo.onrender.com", "User")
```

**Rebuild `$hdr` after every token change.** A hashtable captures the value at
creation, so refreshing `$env:CCP_ADMIN_JWT` alone keeps sending the expired
one. This has caused mid-batch 401s more than once.

Wake the service before any batch. It is free-tier Render and cold-starts past
the 60-second client timeout.

```powershell
curl.exe -s -o NUL -w "%{http_code}`n" "$env:CCP_API_BASE/api/admin/exam-intelligence-cms/pyq-papers?limit=1" -H "Authorization: Bearer $env:CCP_ADMIN_JWT"
```

200 = ready. 401 = refresh the token. No output = still waking; wait and repeat.

---

## Language traps

**`$pid` is a built-in** and cannot be assigned. So are `$host`, `$input`,
`$args`, `$error`, `$home`, `$pwd`. Use `$paper`, `$examId`, and so on.

**`$y:` parses as a drive reference.** Write `${y}` when a variable is followed
by a colon.

**Placeholders in pasted commands run literally.** `--paper-id <the-id>` fails
with *"The '<' operator is reserved for future use"* — PowerShell reads `<` as
redirection. Substitute the real value before pressing enter.

**Connection strings must be on one line.** A URI wrapped across two lines puts
a newline inside the database name: `database "postgres\n" does not exist`.
Assign to `$PG` once and reuse.

**`Select-String` has no `-Recurse` or `-Include`.** Pipe from `Get-ChildItem`.

**`rg` globs fail on Windows paths.** `rg -n "x" app/supabase/migrations/270*.sql`
raises *"The filename, directory name, or volume label syntax is incorrect"*.
Pipe a file list instead:

```powershell
rg -n "microtopic_id" (Get-ChildItem app\supabase\migrations\*.sql |
  Where-Object { $_.Name -match '^27[01]' } | ForEach-Object { $_.FullName })
```

**`head`, `tail` and `wc` do not exist.** Use `Select-Object -First n`,
`-Last n`, `Measure-Object -Line`.

**Escaped quotes inside `python -c` get mangled.** Anything with nested quotes
should go to a file first:

```powershell
@'
import csv
...
'@ | Set-Content script.py -Encoding utf8
python script.py
```

---

## Files

**Downloads arrive as `name (1).csv`** when the target already exists, and the
copy command then takes the stale one. Check before copying:

```powershell
Get-ChildItem "D:\Users\user\Downloads\name*.csv" | Select-Object Name, Length, LastWriteTime
```

**A re-presented file may serve from cache.** If a corrected download keeps
arriving with the old contents, ask for it under a new filename.

**Strip the suffix on arrival:**

```powershell
Get-ChildItem "tags-*draft*.csv" | ForEach-Object {
  $clean = $_.Name -replace ' \(\d+\)', ''
  if ($_.Name -ne $clean) { Rename-Item $_.FullName $clean -Force }
}
```

**Verify a file is the version you think it is** before generating anything from
it. A migration built from a stale CSV runs cleanly and produces the wrong
result — this happened with the Mains rename, where 58 rows silently lost their
prefix.

```powershell
Select-String -Path workbench\catalogs\file.csv -Pattern "expected-string" | Measure-Object -Line
```

**`-Raw` and `-NoNewline` for in-place rewrites**, or PowerShell rewrites line
endings and appends a trailing newline:

```powershell
(Get-Content file.csv -Raw) -replace 'old','new' | Set-Content file.csv -NoNewline
```

---

## Supabase SQL editor

**It auto-commits per execution.** An explicit `begin;` opens a nested
transaction that is discarded, and every statement inside silently rolls back
reporting success. Run multi-statement work through `psql -f`, or paste
statements individually with no wrapper.

**"Success. No rows returned" is ambiguous.** It is what an UPDATE prints
whether it changed eighty rows or none. Always follow with a SELECT.

**Display caps at 100 rows.** Anything larger needs `\copy`:

```powershell
psql $PG -c "\copy (SELECT ...) TO 'out.csv' WITH CSV HEADER"
```

**Long UUIDs pasted into a query get truncated** mid-string, producing
*"unterminated quoted string"* or a stray `limit 100`. Put multi-UUID filters in
a subquery, or run one id at a time.

**Large scripts fail silently on paste** — roughly 1,300 lines produced no
output and no error. Split into ~40 KB chunks, each ending with a count.

**`\copy` writes in the client encoding.** Without `$env:PGCLIENTENCODING =
"UTF8"` an em-dash comes back as byte `0x97` and Python raises
`UnicodeDecodeError`. If you inherit such a file, read it with `encoding='cp1252'`.

---

## Counting

**Joining a child table multiplies the parent.** A question with four options
counted through a join to `pyq_options` reports four times. A CSAT question with
a primary and a secondary tag counts twice. Symptoms look like corrupt data:
"194 questions" on a 97-question paper, "160" on an 80-question one.

Use `count(DISTINCT q.id)`, or aggregate before joining.

**Scoping by `exam_phase_id` silently drops papers.** `exam_phases` has no
unique constraint; five phases named "Prelims" exist for UPSC alone, and the
nine GS-I papers span two of them. Scope by paper id, or by exam plus an
explicit phase list.

**Filter out the fixtures.** `mock_question_bank` holds 140 rows under
`ssc-cgl-legacy-sandbox-do-not-use` and 176 phantom rows on test exam
`22222222-…`. Any unfiltered `count(*)` is 316 too high.

---

## API

**The summary endpoint takes a slug, not an exam id.** Passing a UUID returns
zero papers with no error:

```powershell
Invoke-RestMethod -Uri "$env:CCP_API_BASE/api/exam-intelligence/exams/upsc-cse/pyq-summary" -Headers $hdr
```

**Each paper carries `paper_id`, not `id`.** Using `.id` yields `$null`, the URL
resolves to `.../pyq-papers//preview`, and you get a 404.

**Read the error body on a 4xx.** `$_.ErrorDetails.Message` is empty on
PowerShell 5.1:

```powershell
try { Invoke-RestMethod ... } catch {
  $r = $_.Exception.Response.GetResponseStream()
  (New-Object IO.StreamReader($r)).ReadToEnd()
}
```

That is how a bare 422 became *"Field required: body"*.

**Interpolation inside a loop needs `$($x.prop)`.** `"$s -> ..."` where `$s` is
a loop variable in a `foreach` over strings works; `"$p.paper_id"` does not.

**Refresh the token immediately before any `--apply` or batch run.** The JWT
expires roughly hourly, and a 200-request apply that dies halfway leaves a paper
partially written.

---

## Order of operations that has proved safe

1. Dry-run first. Every apply path has one; it caught a stale microtopic id that
   would have failed at write time.
2. Read the plan. Counts should match what you expect before confirming.
3. Apply.
4. Verify with a SELECT — never trust the tool's own summary alone.
5. If the work changed tags or difficulty on a projected paper, re-sync the
   projection. The content hash changed, so the bank row is now stale and
   demoted to draft.
## Per-paper review sequence

Questions and their primary tags are TWO separate review gates. The
projection counts only verified primary tags, so verifying questions
alone leaves everything blocked.

1. Read the paper, fill `decision` on question rows
2. apply
3. Re-export — tags now reflect the verified questions
4. Sweep, fill `decision=verified` on tag rows whose question verified
5. apply
6. Promote the paper (POST .../pyq-papers/{id}/review)
7. Project (POST .../projection/sync)

## Traps found 2026-09-06/07

`Set-Content -Encoding utf8` writes a BOM. In a generated .sql file it
breaks the first statement — two `begin;` wrappers were silently
dropped this way. Use:
  [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding $false))

`.venv` has `requests`; `.venv-graphify` does not. Export and apply
fail from the wrong one.

`sweep` overwrites a filled worksheet — writes blank by design, never
merges. Copy before re-sweeping. Cost 181 applied grades once.

The direct DB host is IPv6-only and unreachable. Use the Session
pooler URI from the dashboard Connect panel, verbatim.

Direct SQL bypasses the 10-minute exam cache; an is_active change
won't surface in the API until the TTL expires.

CMS list routes cap limit at 200; above that they 422.

Nested quotes in `python -c` fail on PS 5.1. Write to a .py file.

Agent artifacts exist only in the agent's container. "Shipped" in a
report means written there, not committed. Require the commit before
the report — the NABARD extraction was lost this way once.

## Counting

**Joining a child table multiplies the parent.** A question with four options
counted through a join to `pyq_options` reports four times. A CSAT question with
a primary and a secondary tag counts twice. The symptom looks like corrupt data:
"194 questions" on a 97-question paper, "160" on an 80-question one.

Use `count(DISTINCT q.id)`, or aggregate before joining.

**Scoping by `exam_phase_id` silently drops papers.** `exam_phases` has no
unique constraint; five phases named "Prelims" exist for UPSC alone, and the nine
GS-I papers span two of them. Scope by paper id, or by exam plus an explicit
phase list.

**Filter out the fixtures.** `mock_question_bank` holds 140 rows under
`ssc-cgl-legacy-sandbox-do-not-use` and 176 phantom rows on test exam
`22222222-…`. Any unfiltered `count(*)` is 316 too high.

---

## Conflicted CSVs

**A conflicted CSV does not announce itself.** A failed `git stash pop` leaves
`<<<<<<< Updated upstream`, `=======` and `>>>>>>> Stashed changes` inside the
file. `Import-Csv` then reads the marker line as the header and returns twice the
rows under one nonsense column — no error, no warning.

Check any worksheet that has been through a stash or merge:

```powershell
Select-String -Path file.csv -Pattern '^(<<<<<<<|=======|>>>>>>>)' | Select-Object LineNumber
```

Three hits means two versions concatenated: the real header is the line after the
first marker, and the second copy starts after `=======`.

---

## API specifics

**The summary endpoint takes a slug, not an exam id.** Passing a UUID returns
zero papers with no error — which reads as "this exam has nothing" rather than
"wrong argument":

```powershell
Invoke-RestMethod -Uri "$env:CCP_API_BASE/api/exam-intelligence/exams/upsc-cse/pyq-summary" -Headers $hdr
```

**Each paper in that response carries `paper_id`, not `id`.** Using `.id` yields
`$null`, the URL resolves to `.../pyq-papers//preview`, and you get a 404.

**Read the error body on a 4xx.** `$_.ErrorDetails.Message` is empty on
PowerShell 5.1:

```powershell
try { Invoke-RestMethod ... } catch {
  $r = $_.Exception.Response.GetResponseStream()
  (New-Object IO.StreamReader($r)).ReadToEnd()
}
```

That is how a bare 422 became *"Field required: body"*.

**Interpolation inside a loop needs `$($x.prop)`.** `"$s -> ..."` works where
`$s` is a loop variable over strings; `"$p.paper_id"` does not.

---

## Projections

**Tagging a projected paper takes it offline.** Changing a tag or a difficulty
value changes the content hash, which marks the projection stale, which demotes
the bank row to `draft`
(`183_pyq_mock_projection_bridge.sql:855-898`). Practice availability drops until
you re-sync.

**So `reviewer_status='draft'` on a bank row may mean "was invalidated", not
"unreviewed".** 872 of 906 regulatory rows were in that state on 2026-09-05 with
every paper and every question verified. **The fix is a re-sync, never a
promotion** — bulk-promoting draft bank rows publishes stale content.

Preview first; it makes no writes and reports what would change.

```powershell
Invoke-RestMethod -Uri "$env:CCP_API_BASE/api/admin/mocks/pyq-papers/$paper/projection/preview" -Headers $hdr |
  Select-Object total, eligible_count, ineligible_count, would_update_count

$body = @{ audit_reason = "<why>" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$env:CCP_API_BASE/api/admin/mocks/pyq-papers/$paper/projection/sync" -Headers $hdr2 -Body $body
```

---

## Where a review reason can actually live

**`reviewer_notes` is dropped server-side.** The review RPC takes no notes
parameter and `pyq_question_topic_tag` is `supports_notes=False`. The tool sends
it anyway and says so at line 127. The worksheet CSV is the only place it
survives — which is why 101 regulatory corrections carry a status and no recorded
cause.

**The review endpoint writes no audit row at all.** It calls
`update_pyq_question_review_atomic` and returns; there is no `_audit()` on that
branch. So a zero count on `new_value->'patch'->>'reviewer_status'` is expected
whether the status was set through the API or by SQL — **it is not evidence of a
direct SQL write.**

For a durable per-question cause, patch `pyq_questions.metadata` through the CMS
route, which does audit:

```
PATCH /api/admin/exam-intelligence-cms/pyq-questions/{id}
{"reason": "<why>", "payload": {"metadata": {...existing..., "review_cause": "<cause>"}}}
```

**`metadata` is a whole-column replace.** Read the row, merge, then write — or
the patch silently drops every other key.

---

## One habit worth more than any item above

**An absence is not evidence until you know what would produce it.**

Zero audit rows for the 101 regulatory corrections looked like proof they were
written by direct SQL. They were not: the review endpoint does not audit, so zero
is what you get either way. The query could not distinguish the two cases, and was
read as though it could.

Every defect in this corpus has that shape. A validator that reads what is
present will never report what is missing; a query that cannot fail will never
tell you it failed. Before concluding from an empty result, establish what a
non-empty one would have required.