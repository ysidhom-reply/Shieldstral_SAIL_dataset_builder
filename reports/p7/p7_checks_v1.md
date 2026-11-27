# Report: p7_checks_v1

Scope: `bipia` (train, validation, test), `nemotron` (test). P7 only reads data/out and writes this report.

## Checks

| part | config | check | result | detail | examples |
|---|---|---|---|---|---|
| 1 | bipia | load: columns and types match the contract | PASS | 29 columns |  |
| 1 | bipia | load: row counts match the P4/P5 reports | PASS | train 19,480, validation 3,520, test 43,100 |  |
| 1 | nemotron | load: columns and types match the contract | PASS | 29 columns |  |
| 1 | nemotron | load: row counts match the P4/P5 reports | PASS | test 2,288 |  |
| 1 | bipia | splits: no clean document in two splits | PASS | 879 clean documents (sha256 of the clean text) |  |
| 1 | bipia | splits: no train/validation attack text in test | PASS | 21,550 test positives vs 125 train/validation attack texts |  |
| 1 | bipia | splits: no train/validation attack_name in test | PASS | 125 test attack names; allowed by team decision (same names, different strings): Language Translation-0, Language Translation-1, Language Translation-2, Language Translation-3, Language Translation-4 |  |
| 1 | bipia | positives: contain their injection_span exactly | PASS | 33,050 positives, at insert_index |  |
| 1 | nemotron | positives: contain their injection text | PASS | 1,144 positives; compared in decoded form (JSON strings) |  |
| 1 | bipia | negatives: no attack text | PASS | 33,050 negatives vs 250 attacks (Aho-Corasick) |  |
| 1 | nemotron | negatives: no injection text, goal sentence or wrapper marker | PASS | 1,144 negatives; goals from the raw records (1,272) |  |
| 1 | bipia | balance: classes 1:1 within each scenario | PASS | per split and scenario |  |
| 1 | bipia | balance: positions within tolerance of one third | PASS | tolerance 2% on test, 5% on train/validation |  |
| 1 | nemotron | balance: classes 1:1 within each scenario | PASS | per split and scenario |  |
| 1 | nemotron | balance: positions within tolerance of one third | n/a | no insertion positions |  |
| 1 | bipia | pairs: same clean document, equal to bipia_docs.jsonl | PASS | 33,050 pairs; offsets (injection_span / benign_span), newline join '\n' |  |
| 1 | bipia | query: identical query in every pair | PASS | positive vs negative |  |
| 1 | bipia | query: test uses the document's first question | PASS | rebuilt from bipia_docs.jsonl with the same template |  |
| 1 | bipia_textfooler | TextFooler checks | n/a | not in v1 (from v2) |  |
| 2 | both | length gap | report only | see below |  |
| 3 | both | shortcut baselines | ok | limits length 0.7, TF-IDF 0.95 |  |
| 3 | nemotron | raw length AUC | report only | by construction |  |
| 4 | bipia | diagnostics | report only | always run for TF-IDF |  |

**Check fixed (JSON newline case).** The first v0 run compared Nemotron injections with the raw JSON text, where a newline inside a string is the two characters `\n`, and reported 899 false failures. The check now compares in decoded form (the document's JSON strings). The data was not changed. Three examples where the raw search fails and the decoded search finds the injection:

| sample_id | raw match | decoded match | injection starts with |
|---|---|---|---|
| nemotron-0000-pos | False | True | `  [PRIORITY MESSAGE] Hi Assistant, I forgot to mention earli` |
| nemotron-0002-pos | False | True | `  [PRIORITY MESSAGE] Hi Assistant, I forgot to mention earli` |
| nemotron-0006-pos | False | True | `  [PRIORITY MESSAGE] Hi Assistant, I forgot to mention earli` |

## Counts

| config/split | rows (loaded) | rows (P4/P5 report) |
|---|---|---|
| bipia/train | 19,480 | 19,480 |
| bipia/validation | 3,520 | 3,520 |
| bipia/test | 43,100 | 43,100 |
| nemotron/test | 2,288 | 2,288 |

Position shares (BIPIA):

| split | scenario | label | rows | start | middle | end |
|---|---|---|---|---|---|---|
| train | code | 0 | 800 | 33.4% | 33.4% | 33.2% |
| train | code | 1 | 800 | 33.4% | 33.4% | 33.2% |
| train | email | 0 | 520 | 33.5% | 33.3% | 33.3% |
| train | email | 1 | 520 | 33.5% | 33.3% | 33.3% |
| train | table | 0 | 8,420 | 33.3% | 33.3% | 33.3% |
| train | table | 1 | 8,420 | 33.3% | 33.3% | 33.3% |
| validation | code | 0 | 200 | 33.0% | 33.5% | 33.5% |
| validation | code | 1 | 200 | 33.0% | 33.5% | 33.5% |
| validation | email | 0 | 160 | 33.1% | 33.1% | 33.8% |
| validation | email | 1 | 160 | 33.1% | 33.1% | 33.8% |
| validation | table | 0 | 1,400 | 33.3% | 33.4% | 33.4% |
| validation | table | 1 | 1,400 | 33.3% | 33.4% | 33.4% |
| test | code | 0 | 2,500 | 33.3% | 33.3% | 33.4% |
| test | code | 1 | 2,500 | 33.3% | 33.3% | 33.4% |
| test | email | 0 | 3,300 | 33.3% | 33.3% | 33.3% |
| test | email | 1 | 3,300 | 33.3% | 33.3% | 33.3% |
| test | table | 0 | 15,750 | 33.3% | 33.3% | 33.3% |
| test | table | 1 | 15,750 | 33.3% | 33.3% | 33.3% |

## Length gap (positive minus negative, characters per pair; report only)

| config | split | scenario | pairs | median | 5th pct | 95th pct |
|---|---|---|---|---|---|---|
| bipia | test | code | 2,500 | -6 | -243 | 177 |
| bipia | test | email | 3,300 | 6 | -9 | 59 |
| bipia | test | table | 15,750 | 4 | -15 | 51 |
| bipia | train | code | 800 | 4 | -103 | 147 |
| bipia | train | email | 520 | 1 | -7 | 36 |
| bipia | train | table | 8,420 | 2 | -9 | 26 |
| bipia | validation | code | 200 | 3 | -85 | 149 |
| bipia | validation | email | 160 | 2 | -6 | 30 |
| bipia | validation | table | 1,400 | 2 | -8 | 29 |
| nemotron | test | ecommerce | 157 | 279 | 137 | 427 |
| nemotron | test | education | 150 | 351 | 233 | 453 |
| nemotron | test | healthcare | 183 | 304 | 151 | 428 |
| nemotron | test | hr | 131 | 344 | 170 | 436 |
| nemotron | test | insurance | 65 | 345 | 192 | 431 |
| nemotron | test | it_helpdesk | 91 | 350 | 212 | 438 |
| nemotron | test | legal | 49 | 343 | 216 | 374 |
| nemotron | test | logistics | 158 | 344 | 181 | 436 |
| nemotron | test | real_estate | 160 | 377 | 204 | 432 |

Nemotron is large by design: the negative is the positive with the injection cut out. Not a failure.

## Shortcut baselines (trained on BIPIA train only, seed 42; no detector)

- Length: logistic regression on log(characters) and log(words) of the document. Limit: AUC 0.7, per BIPIA scenario.
- TF-IDF: `TfidfVectorizer` fitted on the train documents only (word 1-2-grams, min_df 5, sublinear_tf, at most 50,000 features) + logistic regression (C = 1.0). Limit: AUC 0.95, per BIPIA scenario and on Nemotron (pooled).
- Recall at 1% FPR: threshold = the 99th percentile of the negatives' scores; recall = share of positives scoring above it.
- The limits are read from `configs/config.yaml` (the team's choice); P7 never edits them. A limit is exceeded when AUC > limit.

| baseline | evaluated on | scenario | rows | AUC | recall at 1% FPR | limit | status |
|---|---|---|---|---|---|---|---|
| length | bipia test | all | 43,100 | 0.507 | 0.8% | - | report |
| length | bipia test | code | 5,000 | 0.585 | 2.8% | 0.7 | ok |
| length | bipia test | table | 31,500 | 0.499 | 0.8% | 0.7 | ok |
| length | bipia test | email | 6,600 | 0.506 | 1.2% | 0.7 | ok |
| length | bipia test without Language Translation | all | 40,560 | 0.508 | 0.8% | - | report |
| length | bipia test without Language Translation | code | 5,000 | 0.585 | 2.8% | - | report |
| length | bipia test without Language Translation | table | 29,400 | 0.499 | 0.7% | - | report |
| length | bipia test without Language Translation | email | 6,160 | 0.507 | 1.2% | - | report |
| tfidf | bipia test | all | 43,100 | 0.916 | 35.8% | - | report |
| tfidf | bipia test | code | 5,000 | 0.944 | 41.4% | 0.95 | ok |
| tfidf | bipia test | table | 31,500 | 0.943 | 63.4% | 0.95 | ok |
| tfidf | bipia test | email | 6,600 | 0.819 | 7.0% | 0.95 | ok |
| tfidf | bipia test without Language Translation | all | 40,560 | 0.914 | 34.8% | - | report |
| tfidf | bipia test without Language Translation | code | 5,000 | 0.944 | 41.4% | - | report |
| tfidf | bipia test without Language Translation | table | 29,400 | 0.941 | 63.1% | - | report |
| tfidf | bipia test without Language Translation | email | 6,160 | 0.814 | 6.8% | - | report |
| tfidf | nemotron test | all | 2,288 | 0.782 | 2.0% | 0.95 | ok |
| tfidf | nemotron test | healthcare | 366 | 0.823 | 26.8% | - | report |
| tfidf | nemotron test | ecommerce | 314 | 0.799 | 7.6% | - | report |
| tfidf | nemotron test | legal | 98 | 0.776 | 12.2% | - | report |
| tfidf | nemotron test | insurance | 130 | 0.882 | 3.1% | - | report |
| tfidf | nemotron test | education | 300 | 0.782 | 2.0% | - | report |
| tfidf | nemotron test | real_estate | 320 | 0.755 | 1.9% | - | report |
| tfidf | nemotron test | logistics | 316 | 0.903 | 26.6% | - | report |
| tfidf | nemotron test | it_helpdesk | 182 | 0.740 | 2.2% | - | report |
| tfidf | nemotron test | hr | 262 | 0.661 | 3.1% | - | report |

**Nemotron length (report only).** Score = the raw document length in characters (longer = attack); AUC over the Nemotron test rows of each domain and pooled over all of them. *Positive longer* = share of pairs in which the positive's document is longer than its negative's. A BIPIA-trained length model carries no meaning on Nemotron, so none is reported.

| domain | pairs | AUC (raw length) | positive longer |
|---|---|---|---|
| all | 1144 | 0.985 | 100.0% |
| healthcare | 183 | 0.978 | 100.0% |
| ecommerce | 157 | 0.975 | 100.0% |
| legal | 49 | 0.995 | 100.0% |
| insurance | 65 | 0.987 | 100.0% |
| education | 150 | 0.996 | 100.0% |
| real_estate | 160 | 0.996 | 100.0% |
| logistics | 158 | 0.996 | 100.0% |
| it_helpdesk | 91 | 0.995 | 100.0% |
| hr | 131 | 0.982 | 100.0% |

## Diagnostics (TF-IDF on BIPIA)

### 1. Strongest features per scenario and class

Strength = weight x mean tf-idf in that scenario and class (test). Origin (letter after the feature): `i` = injection_span, `b` = benign_span (at least half of the train rows containing the feature have it inside their inserted span), `d` = the surrounding document.

**code, label 1** (document 2, injection_span 28): `your` (i), `code` (i), `import` (i), `the following` (i), `requests` (i), `in your` (i), `following` (i), `com` (i), `of` (i), `response` (i), `example` (i), `subprocess` (i), `in` (i), `following code` (i), `to` (i), `with` (i), `an` (i), `os` (i), `import requests` (i), `write` (i), `example com` (i), `your response` (i), `the` (i), `file` (i), `how` (i), `do` (i), `you` (d), `code snippet` (i), `is` (d), `socket` (i)

**code, label 0** (benign_span 8, document 22): `print` (b), `python` (b), `this` (d), `if` (d), `as` (d), `python import` (b), `error` (d), `it` (d), `not` (d), `if you` (d), `list` (d), `you can` (d), `be` (d), `try` (d), `np` (d), `all` (d), `when` (b), `or` (d), `from` (d), `was` (d), `some` (b), `to the` (d), `note` (b), `have` (d), `in the` (b), `array` (d), `by` (d), `each` (b), `two` (d), `set` (d)

**email, label 1** (document 2, injection_span 28): `your` (i), `reply` (i), `com` (i), `your response` (i), `response` (i), `in your` (i), `of` (i), `your answer` (i), `answer` (i), `in` (i), `your reply` (i), `to` (i), `how` (i), `with` (i), `you` (d), `from your` (i), `provide` (i), `the` (i), `an` (i), `hello` (d), `what` (i), `using` (i), `for` (i), `add` (i), `thank` (i), `thank you` (i), `to your` (i), `are the` (i), `about` (i), `modify your` (i)

**email, label 0** (benign_span 1, document 29): `this` (d), `was` (d), `if` (d), `mercury` (d), `account` (d), `if you` (d), `please` (b), `have` (d), `you have` (d), `has` (d), `from` (d), `charge` (d), `payment` (d), `received` (d), `your company` (d), `by` (d), `company` (d), `any questions` (d), `be` (d), `been` (d), `transaction` (d), `any` (d), `have any` (d), `over` (d), `hi` (d), `to this` (d), `this email` (d), `has been` (d), `you can` (d), `checking` (d)

**table, label 1** (document 1, injection_span 29): `your` (i), `your response` (i), `response` (i), `in your` (i), `of` (i), `your answer` (i), `answer` (i), `your reply` (i), `reply` (i), `in` (i), `provide` (i), `how` (i), `what` (i), `to` (i), `about` (i), `with` (i), `the` (i), `modify your` (i), `modify` (i), `com` (i), `integrate` (i), `add` (i), `are the` (i), `what are` (i), `of the` (d), `provide your` (i), `sentence` (i), `enhance your` (i), `enhance` (i), `answer to` (i)

**table, label 0** (benign_span 12, document 18): `column` (b), `total` (d), `not` (d), `were` (b), `all` (d), `this` (d), `team` (d), `games` (d), `table` (b), `season` (d), `to the` (d), `year` (d), `note` (b), `by` (d), `please` (b), `time` (d), `when` (b), `in the` (b), `results` (b), `gold` (d), `was` (d), `no` (d), `note that` (b), `rank` (d), `positions` (b), `some` (b), `as` (d), `column for` (b), `it` (d), `place` (d)

### 2. Ablation: without the 50 strongest negative-class features from benign_span

Dropped: `were`, `print`, `please`, `results`, `reflect`, `some`, `figures`, `marked`, `each`, `listed`, `during`, `source`, `python import`, `multiple`, `column`, `recorded`, `where`, `noted`, `confirm`, `matches`, `athletes`, `reflects`, `entries`, `due to`, `note`, `table`, `indicate`, `across`, `years`, `scores`, `due`, `rounded`, `included`, `note that`, `events`, `rounded to`, `python`, `after`, `in the`, `positions`, `includes`, `standard`, `times`, `conditions`, `when`, `names`, `medals`, `totals`, `top`, `held`

| scenario | AUC before | AUC after | drop |
|---|---|---|---|
| all | 0.916 | 0.895 | 0.021 |
| code | 0.944 | 0.928 | 0.015 |
| table | 0.943 | 0.927 | 0.016 |
| email | 0.819 | 0.799 | 0.020 |

### 3. Span-only view (TF-IDF trained and tested on the inserted spans only)

| scenario | AUC | recall at 1% FPR |
|---|---|---|
| all | 0.968 | 72.2% |
| code | 0.995 | 94.0% |
| table | 0.981 | 88.0% |
| email | 0.911 | 46.7% |

### 4. Words over-represented in the benign spans

Log-odds ratio (benign spans vs injected spans, all BIPIA rows) with add-0.5 smoothing, divided by its standard error (z).

| word | z | count in benign spans | count in injected spans |
|---|---|---|---|
| python | 44.2 | 3,696 | 440 |
| if | 31.9 | 2,021 | 351 |
| when | 31.6 | 2,010 | 140 |
| check | 25.8 | 1,266 | 140 |
| for | 22.2 | 6,383 | 4,575 |
| make | 20.2 | 900 | 211 |
| any | 18.8 | 723 | 140 |
| this | 18.0 | 2,823 | 1,805 |
| before | 17.6 | 857 | 281 |
| each | 16.3 | 754 | 254 |
| may | 15.1 | 508 | 120 |
| or | 14.9 | 952 | 444 |
| details | 14.1 | 657 | 254 |
| home | 14.1 | 434 | 100 |
| it | 13.7 | 521 | 170 |
| def | 13.7 | 591 | 220 |
| first | 13.6 | 670 | 279 |
| values | 13.5 | 395 | 20 |
| result | 13.3 | 405 | 100 |
| performance | 13.3 | 341 | 50 |

Share of negatives whose benign span contains at least one of these 20 words: **44.4%** (injected spans: 24.2%).

### 5. AUC by position and by attack group (TF-IDF, BIPIA test)

| position | rows | AUC |
|---|---|---|
| all | 43,100 | 0.916 |
| start | 14,366 | 0.919 |
| end | 14,368 | 0.916 |
| middle | 14,366 | 0.913 |

| attack group | rows | AUC |
|---|---|---|
| Base Encoding | 2,540 | 0.952 |
| Blocking Internet Connection | 500 | 0.941 |
| Bringing Down Hosts and Servers (Denial of Service) | 500 | 0.916 |
| Business Intelligence | 2,540 | 0.861 |
| Compromising Computers | 500 | 0.974 |
| Conversational Agent | 2,540 | 0.744 |
| Corrupting an Operating System | 500 | 0.872 |
| Data Eavesdropping | 500 | 0.979 |
| Emoji Substitution | 2,540 | 0.988 |
| Encrypting Documents and Demanding Ransom (Ransomware) | 500 | 0.985 |
| Entertainment | 2,540 | 0.993 |
| Information Dissemination | 2,540 | 0.994 |
| Introduce System Fingerprinting | 500 | 0.958 |
| Keylogging | 500 | 0.959 |
| Language Translation | 2,540 | 0.959 |
| Marketing & Advertising | 2,540 | 0.995 |
| Misinformation & Propaganda | 2,540 | 0.991 |
| Research Assistance | 2,540 | 0.870 |
| Reverse Text | 2,540 | 0.950 |
| Scams & Fraud | 2,540 | 0.994 |
| Screen Scraping | 500 | 0.926 |
| Sentiment Analysis | 2,540 | 0.701 |
| Substitution Ciphers | 2,540 | 0.979 |
| Task Automation | 2,540 | 0.848 |
| Traffic Analysis | 500 | 0.941 |
| all | 43,100 | 0.916 |

### 7. Hard negatives (P3 option B)

Share of negatives whose benign text is a hard negative (a harmless text that sounds like an instruction to the human reader), and TF-IDF on BIPIA test with only hard-negative or only standard negatives (all positives kept).

| split | email | table | code |
|---|---|---|---|
| test | 40.0% | 40.0% | 33.6% |
| train | 42.1% | 9.9% | 22.0% |
| validation | 33.8% | 8.5% | 30.5% |

| negatives | scenario | AUC | recall at 1% FPR |
|---|---|---|---|
| hard_negative | all | 0.905 | 22.0% |
| hard_negative | code | 0.899 | 32.0% |
| hard_negative | table | 0.954 | 66.9% |
| hard_negative | email | 0.729 | 5.4% |
| standard | all | 0.923 | 50.4% |
| standard | code | 0.966 | 60.7% |
| standard | table | 0.936 | 62.1% |
| standard | email | 0.879 | 22.8% |

### 6. Options (none applied)

- (a) choose the pool text per pair in P4 so that it avoids the over-used words, with no new API calls;
- (b) regenerate part of the P3 pool with more variety in style, including harmless texts that sound like instructions;
- (c) change a limit, with a written reason (team decision; P7 never edits a limit).

## Note on Language Translation

Team decision: the attack names `Language Translation-0..4` stay in test although the same names exist in train (their strings differ). The attack_name check reports them as allowed. An evaluation without them could be interesting: the baselines are also reported on BIPIA test without Language Translation (report only).

## Result

PUSH ALLOWED
