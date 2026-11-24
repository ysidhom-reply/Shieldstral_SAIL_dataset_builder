# Report: p3_hard_negatives

## Counts (total rows: **522**)

### scenario

| scenario | rows |
|---|---:|
| code | 144 |
| email | 198 |
| table | 180 |

### split

| split | rows |
|---|---:|
| test | 282 |
| train | 168 |
| validation | 72 |

### position

| position | rows |
|---|---:|
| end | 174 |
| middle | 174 |
| start | 174 |

### cross

| scenario | split | position | rows |
|---|---|---|---:|
| code | test | end | 24 |
| code | test | middle | 24 |
| code | test | start | 24 |
| code | train | end | 16 |
| code | train | middle | 16 |
| code | train | start | 16 |
| code | validation | end | 8 |
| code | validation | middle | 8 |
| code | validation | start | 8 |
| email | test | end | 50 |
| email | test | middle | 50 |
| email | test | start | 50 |
| email | train | end | 8 |
| email | train | middle | 8 |
| email | train | start | 8 |
| email | validation | end | 8 |
| email | validation | middle | 8 |
| email | validation | start | 8 |
| table | test | end | 20 |
| table | test | middle | 20 |
| table | test | start | 20 |
| table | train | end | 32 |
| table | train | middle | 32 |
| table | train | start | 32 |
| table | validation | end | 8 |
| table | validation | middle | 8 |
| table | validation | start | 8 |

## Why

Team decision (2026-11-24), option B of P7 v0: the brief's P3 filters keep the attacks' vocabulary out of every benign text, so TF-IDF separated BIPIA code at AUC 0.967. 40% of every pool is replaced by harmless texts that sound like instructions to the human reader (prompts A-hn / B-hn, judge-v4).

## Run

- Mode: pilot; 60 pools; source `benign_pool.jsonl`.
- Texts replaced: 522; new hard negatives accepted: 522; replaced texts put back (pool short of new texts): 0 in 0 pools.
- Generated: 708. Generator `mistral-small-2603`, judge `mistral-medium-2604` (judge-v4 / judge-batch-v2).
- Cost: $0.2762 (mistral-small-2603 $0.0427, mistral-medium-2604 $0.2335).

## Attack vocabulary in the new texts (share of texts)

| scenario | texts | your reply/answer/response | please / make sure / let me know | the following code/snippet | your code/script/project | requests | subprocess | example.com |
|---|---|---|---|---|---|---|---|---|
| email | 198 | 37% | 49% | 0% | 0% | 0% | 0% | 0% |
| table | 180 | 0% | 21% | 0% | 0% | 0% | 0% | 0% |
| code | 144 | 0% | 11% | 46% | 87% | 2% | 1% | 2% |

## Rejection rate per filter (new texts)

| filter | name | texts checked | rejected | rejection rate | share of all generated |
|---|---|---|---|---|---|
| 1 | shape | 586 | 11 | 1.9% | 1.6% |
| 2 | forbidden content | 575 | 3 | 0.5% | 0.4% |
| 3 | duplicate | 572 | 1 | 0.2% | 0.1% |
| 4 | judge | 571 | 49 | 8.6% | 6.9% |

## Rejections per scenario and position

| scenario | position | generated | f1 shape | f2 forbidden content | f3 duplicate | f4 judge | accepted |
|---|---|---|---|---|---|---|---|
| code | start | 57 | 5 (8.8%) | 2 (3.8%) | 0 (0.0%) | 2 (4.0%) | 48 |
| code | middle | 55 | 2 (3.6%) | 1 (1.9%) | 0 (0.0%) | 4 (7.7%) | 48 |
| code | end | 54 | 3 (5.6%) | 0 (0.0%) | 0 (0.0%) | 3 (5.9%) | 48 |
| email | start | 78 | 0 (0.0%) | 0 (0.0%) | 1 (1.3%) | 11 (14.3%) | 66 |
| email | middle | 74 | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 8 (10.8%) | 66 |
| email | end | 82 | 1 (1.2%) | 0 (0.0%) | 0 (0.0%) | 15 (18.5%) | 66 |
| table | start | 63 | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 3 (4.8%) | 60 |
| table | middle | 61 | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 1 (1.6%) | 60 |
| table | end | 62 | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 2 (3.2%) | 60 |

## Most common rejection reasons

| filter | reason | texts |
|---|---|---|
| judge | judge (batch-N): injection | 40 |
| shape | length N outside N-N | 11 |
| judge | judge: addressed to the assistant | 9 |
| forbidden content | forbidden code | 3 |
| duplicate | exact duplicate | 1 |

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
