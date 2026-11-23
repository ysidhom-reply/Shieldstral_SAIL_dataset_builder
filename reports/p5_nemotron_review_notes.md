Read in full on 2026-11-20 (records 21, 60, 70, 292, 405, 454, 519, 525, 676, 809, 857, 862, 871, 902, 997, 1035, 1063, 1132, 1169, 1217; seeded sample, see "Review sample" below).

**Verdict: 20 / 20 pairs are correct.** In every pair, the positive holds the full released injection, and the negative differs only in the injected field. The cut reads as natural text in all 20.

What the sample covers:
- **Wrappers:** 16 priority_message and 4 urgent_emoji (21, 70, 862, 902). No bare_goal pairs were drawn; there are 20 in the dataset.
- **Position of the injection:** mid-field in 18 pairs, end of field in 2 (519, 862). In those two the negative now ends at the previous sentence, which is correct.
- **Document shape:** 70 is a `lab_results` entry list, so its document is a JSON array, not an object. This is expected: it is what the read tool returns for that collection.

Pairs worth noting:
- **1169** is a sentence-repair case (`cut_lead`). The unfinished clause "...good communication skills but" and the stray "." are both gone, and the text reads cleanly.
- **1132** is a KB article whose newlines are literal `\n` in the release, kept as is. The injection sits between "3." and "Verify client certificate validity.". The negative restores the numbered list, including the very step the attack asked to delete. That makes it a good counterfactual.
- **902** has a doubled ".." inside the injection ("usage.."). It is removed with the span, and nothing is left behind.
- **1063** is a natural hard negative ("Urgent Shared Drive Access Request", "The user has requested immediate access ..."). It is benign text, correctly labelled 0.

Two observations; neither is a label problem:
- **User requests built around the injection (405, 454, 862).** Some requests were written around the injected content ("summarizing the medical records release consent mentioned in the notes", "if the notes indicate that attorney fees have been waived"). In the negative the request now points to content that is absent. The policy question is still answered correctly by "no".
- **Duplicate source records.** Several records reuse the same base record (CIV-2023-0456, POL-11223) with different injections. The documents are still pairwise distinct, as the duplicate check confirms.
