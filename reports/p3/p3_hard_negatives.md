# Report: p3_hard_negatives

## Counts (total rows: **22616**)

### scenario

| scenario | rows |
|---|---:|
| code | 2396 |
| email | 2136 |
| table | 18084 |

### split

| split | rows |
|---|---:|
| test | 8816 |
| train | 11688 |
| validation | 2112 |

### position

| position | rows |
|---|---:|
| end | 7537 |
| middle | 7540 |
| start | 7539 |

### cross

| scenario | split | position | rows |
|---|---|---|---:|
| code | test | end | 397 |
| code | test | middle | 400 |
| code | test | start | 399 |
| code | train | end | 320 |
| code | train | middle | 320 |
| code | train | start | 320 |
| code | validation | end | 80 |
| code | validation | middle | 80 |
| code | validation | start | 80 |
| email | test | end | 440 |
| email | test | middle | 440 |
| email | test | start | 440 |
| email | train | end | 208 |
| email | train | middle | 208 |
| email | train | start | 208 |
| email | validation | end | 64 |
| email | validation | middle | 64 |
| email | validation | start | 64 |
| table | test | end | 2100 |
| table | test | middle | 2100 |
| table | test | start | 2100 |
| table | train | end | 3368 |
| table | train | middle | 3368 |
| table | train | start | 3368 |
| table | validation | end | 560 |
| table | validation | middle | 560 |
| table | validation | start | 560 |

## Why

Team decision (2026-11-24), option B of P7 v0: the brief's P3 filters keep the attacks' vocabulary out of every benign text, so TF-IDF separated BIPIA code at AUC 0.967. 40% of every pool is replaced by harmless texts that sound like instructions to the human reader (prompts A-hn / B-hn, judge-v4).

## Run

- Mode: full; 2,637 pools; source `benign_pool.jsonl`.
- Texts replaced: 22,616; new hard negatives accepted: 22,616; replaced texts put back (pool short of new texts): 2 in 1 pools.
- Generated: 27,595. Generator `mistral-small-2603`, judge `mistral-medium-2604` (judge-v4 / judge-batch-v2).
- Cost: $7.1013 (mistral-small-2603 $1.1346, mistral-medium-2604 $5.9667).

## Attack vocabulary in the new texts (share of texts)

| scenario | texts | your reply/answer/response | please / make sure / let me know | the following code/snippet | your code/script/project | requests | subprocess | example.com |
|---|---|---|---|---|---|---|---|---|
| email | 2136 | 41% | 52% | 0% | 0% | 0% | 0% | 0% |
| table | 18084 | 0% | 22% | 0% | 0% | 0% | 0% | 0% |
| code | 2396 | 0% | 10% | 41% | 85% | 2% | 1% | 2% |

## Rejection rate per filter (new texts)

| filter | name | texts checked | rejected | rejection rate | share of all generated |
|---|---|---|---|---|---|
| 1 | shape | 24706 | 225 | 0.9% | 0.8% |
| 2 | forbidden content | 24481 | 296 | 1.2% | 1.1% |
| 3 | duplicate | 24185 | 153 | 0.6% | 0.6% |
| 4 | judge | 24032 | 1416 | 5.9% | 5.1% |

## Rejections per scenario and position

| scenario | position | generated | f1 shape | f2 forbidden content | f3 duplicate | f4 judge | accepted |
|---|---|---|---|---|---|---|---|
| code | start | 1079 | 83 (7.7%) | 123 (12.3%) | 0 (0.0%) | 74 (8.5%) | 799 |
| code | middle | 912 | 5 (0.5%) | 29 (3.2%) | 0 (0.0%) | 78 (8.9%) | 800 |
| code | end | 1076 | 40 (3.7%) | 136 (13.1%) | 0 (0.0%) | 103 (11.4%) | 797 |
| email | start | 856 | 2 (0.2%) | 2 (0.2%) | 9 (1.1%) | 131 (15.5%) | 712 |
| email | middle | 921 | 4 (0.4%) | 0 (0.0%) | 10 (1.1%) | 195 (21.5%) | 712 |
| email | end | 864 | 3 (0.3%) | 1 (0.1%) | 12 (1.4%) | 136 (16.0%) | 712 |
| table | start | 6330 | 19 (0.3%) | 1 (0.0%) | 41 (0.6%) | 241 (3.8%) | 6028 |
| table | middle | 6322 | 51 (0.8%) | 2 (0.0%) | 25 (0.4%) | 216 (3.5%) | 6028 |
| table | end | 6346 | 18 (0.3%) | 2 (0.0%) | 56 (0.9%) | 242 (3.9%) | 6028 |

## Most common rejection reasons

| filter | reason | texts |
|---|---|---|
| judge | judge (batch-N): injection | 1161 |
| judge | judge: addressed to the assistant | 255 |
| forbidden content | forbidden code | 226 |
| shape | length N outside N-N | 82 |
| duplicate | near duplicate (ratio N.N) | 78 |
| duplicate | exact duplicate | 75 |
| shape | invalid JSON reply | 64 |
| shape | not one intro + exactly one fenced code block | 63 |
| forbidden content | URL | 58 |
| shape | intro is not one sentence | 11 |
| forbidden content | assistant wording | 6 |
| forbidden content | email address | 6 |
| shape | N sentences (expected N-N) | 5 |

## Judge-v4 positive control

40 of 40 real BIPIA attacks flagged. Required: all.

## Filter profile for the new texts (deviation from the P3 brief)

- Assistant wording: `assistant`, `chatbot`, `language model`, `ignore`, `disregard`, `system prompt`, `pretend`, `as an ai` (`your response` and `your answer` are allowed: addressed to the reader).
- Code-intro patterns ('add the following code', 'make sure your code includes'): not applied; judge-v4 decides.
- Forbidden code: `\bimport\s+(socket|urllib|httpx|aiohttp|smtplib|ftplib|telnetlib|paramiko)\b`, `\bfrom\s+(socket|urllib|http|httpx|aiohttp|smtplib|ftplib)\b`, `\bhttp\.client\b`, `\brequests\.(post|put|patch|delete)\b`, `requests\.\w+\([^)]*\b(data|json|files)\s*=`, `\bshell\s*=\s*True\b`, `\bos\.(system|popen|exec\w*|spawn\w*|fork|kill)\b`, `\bos\.(remove|unlink|rmdir|removedirs)\b`, `\b(shutil\.rmtree|\.unlink\(|\.rmdir\()`, `\bos\.environ\b`, `\bgetenv\b`, `(?<![.\w])(eval|exec|__import__)\s*\(`, `\bctypes\b`, `\b(password|passwd|secret|token|api_key|apikey|credential)s?\b`, `\b(platform|getpass|keyboard|pynput|psutil)\b`
- Allowed: `requests.get` with a timeout on placeholder URLs, `subprocess.run` with a fixed argument list, local file reads. URLs only on `example.com`, `www.example.com`, `example.org` (RFC 2606).
- Unchanged: shape, 8-word overlap with BIPIA attacks, near-duplicates (also against the kept texts), length, size cap and top-up.

## Checks

- PASSED: every pool keeps its size
- PASSED: benign_id unique
- PASSED: judge-v4 flags every positive-control attack
