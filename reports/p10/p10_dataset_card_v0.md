---
pretty_name: ipi-twins
license: cc-by-sa-4.0
language:
- en
task_categories:
- text-classification
tags:
- prompt-injection
- indirect-prompt-injection
- llm-security
- guardrails
size_categories:
- 10K<n<100K
configs:
- config_name: bipia
  data_files:
  - split: train
    path: bipia/train.parquet
  - split: validation
    path: bipia/validation.parquet
  - split: test
    path: bipia/test.parquet
  default: true
- config_name: nemotron
  data_files:
  - split: test
    path: nemotron/test.parquet
---

# ipi-twins

Tool outputs for training and benchmarking guards against **indirect prompt injection**: an instruction hidden in a document that an AI assistant reads. Every attacked sample (label 1) has a benign *twin* (label 0): the same document and query, with a harmless text inserted at the same place instead of the attack. Only the inserted text differs, so a classifier cannot learn shortcuts from the document, the query or the position.

**Version `v0`.** Configs: `bipia`, `nemotron`. Not yet included: `bipia_textfooler` (TextFooler variants, added in v1).

## Load

```python
from datasets import load_dataset

ds = load_dataset("SAIL-Reply/ipi-twins", "bipia", split="train", revision="v0")
ds = load_dataset("SAIL-Reply/ipi-twins", "bipia", split="validation", revision="v0")
ds = load_dataset("SAIL-Reply/ipi-twins", "bipia", split="test", revision="v0")
ds = load_dataset("SAIL-Reply/ipi-twins", "nemotron", split="test", revision="v0")
```

The repository is private: log in with a token that can read it.

## Sources

| source | used for | licence |
|---|---|---|
| [BIPIA](https://github.com/microsoft/BIPIA) (Microsoft), commit `a004b69` | `bipia`: the documents (emails, tables, code answers), their questions and the 250 attacks | MIT; table and code contexts CC BY-SA 4.0 |
| [Nemotron-RL-Agentic-Indirect-Prompt-Injection-v1](https://huggingface.co/datasets/nvidia/Nemotron-RL-Agentic-Indirect-Prompt-Injection-v1) (NVIDIA), revision `d738d4f` | `nemotron`: records with an injection, and the same records without it | CC BY 4.0 |
| Mistral Small (`mistral-small-2603`) and Mistral Medium (`mistral-medium-2604`) | `bipia`: the synthetic benign texts of the negatives (written by Small, judged by Medium) | Apache 2.0 (Small) |

## Splits

### `bipia`

- **Split by document.** The 879 BIPIA documents are divided between train, validation and test, so no document is in more than one split. Test is BIPIA's official test documents plus 126 tables moved from train (to have enough tables); about 10% of each scenario is held out for validation.
- **Attacks follow the split.** Train and validation use only BIPIA's train attacks; test uses only its test attacks.
- **Sampling.** Train and validation: 20 (attack, position) pairs sampled for each document. Test: every document meets every test attack of its scenario, at a balanced random position (start, middle or end).

| split | documents | attacks per document | rows | email | table | code |
|---|---|---|---|---|---|---|
| train | 487 | 20, sampled | 19,480 | 1,040 | 16,840 | 1,600 |
| validation | 88 | 20, sampled | 3,520 | 320 | 2,800 | 400 |
| test | 304 | every test attack (75 text, 50 code) | 43,100 | 6,600 | 31,500 | 5,000 |

Every attacked row has its twin, so rows are half label 1 and half label 0. Test has more rows than train although train has more documents, because test pairs every document with every test attack (a benchmark) while train samples 20 per document.

### `nemotron`

Test only: 2,288 rows (1,144 pairs), capped at 20 pairs per attacker goal so that no goal dominates.

## How `bipia` was built

1. **Attacked samples.** An attack is inserted into a document at the start, the middle (a sentence or line boundary) or the end. The query is the document's question (email, table) or a request to fix the error (code).
2. **Synthetic benign twins.** For every document and position, Mistral Small writes harmless texts that fit the document, and Mistral Medium judges each one (it must not be an injection). A twin is the same document with one of these texts inserted in place of the attack, at the same position and with the same query. About 40% of the texts are *hard negatives*: harmless texts that sound like instructions to the human reader, so a model cannot separate the classes just because attacks use words such as "your reply" or "the following code".
3. **Checks.** Before each push, the data is checked automatically (no document in two splits, every attack present, no attack text in a twin, classes 1:1, positions balanced) and two simple shortcut classifiers (document length, word counts) must stay below fixed limits.

## How `nemotron` was built

Each NVIDIA record becomes a pair: the document with its injection as released (label 1), and the same document with the injection cut out (label 0). Where a cut falls inside a sentence it is repaired by hand-checked rules. No LLM is used.

## Columns

Every config has the same 30 columns, in the same order. A model sees `instruct`, `query` and `document`, and predicts `answer` (`yes` = contains an injection) or `label` (1 / 0).

| column | meaning |
|---|---|
| `sample_id`, `parent_id` | unique id; on a negative, the id of its attacked twin |
| `source`, `split`, `source_split` | config, split here, split in the source |
| `scenario` | BIPIA: email / table / code; Nemotron: the domain |
| `instruct`, `query` | the fixed policy text; the user's request |
| `document` | the tool output, with the attack or the benign text inside |
| `label`, `answer` | 1 / `yes` = contains an injection; 0 / `no` = clean |
| `attack_group`, `attack_name`, `injection_span` | the attack (positives) |
| `position`, `insert_index` | BIPIA: where the text was inserted |
| `benign_id`, `benign_span` | BIPIA negatives: the benign text and its [start, end) in `document` |
| `protected_tokens`, `victim_model` | TextFooler variants only |
| Nemotron metadata | `document_path`, `injection_vector`, `attack_category`, `goal_group`, `wrapper`, `wrapper_outside_span`, `sentence_repaired` |
| `label_provenance`, `generation_method`, `label_checked` | how the row and its label were made |

Columns that do not apply to a row are null.

## Licences and credit

Released under **CC BY-SA 4.0** (share-alike, because it contains CC BY-SA 4.0 material). The repository is private.

- **BIPIA** (Microsoft): MIT. Its table and code contexts are CC BY-SA 4.0.
- **Nemotron-RL-Agentic-Indirect-Prompt-Injection-v1** (NVIDIA): CC BY 4.0, <https://creativecommons.org/licenses/by/4.0/>. Credit: NVIDIA. Changes: records reformatted to this schema, the document cut to the injection's path, a clean counterfactual added per record, records capped per attacker goal.
- **Benign texts** in `bipia` negatives were generated with Mistral Small (Apache 2.0).
- **TextAttack** (MIT) is used for the TextFooler config (added in v1).
