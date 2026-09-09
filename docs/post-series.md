# Post series — drafts

Seven posts, one every three or four days, 150–200 words each. Self-contained: none
of them requires having read the others. Only the last one carries the repo link;
the rest ask for nothing.

Two posts from the original plan are gone. "What it costs not to document a catalog"
died when the measurement came back at $0.0048 — the premise was wrong. "Where the
LLM is a waste of money" died with it, and the replacement is better: money is not
the constraint, reliability is.

Every number below is in the repo and can be checked.

---

## 1. The value is 47382

**Visual:** a single column of bare integers, no header.

> A column in a contact-centre table holds the value `47382`.
>
> Is that 47 seconds or 47 minutes? Both are plausible handle times. One of them is
> wrong by a factor of a thousand.
>
> You can profile that column all day. Count the nulls, check the cardinality, look
> at the distribution, sample a hundred values. None of it answers the question,
> because the answer is not in the data. It is in the vendor's documentation, in a
> sentence that says durations are reported in milliseconds.
>
> This is not a hard problem. It is an impossible one, for anyone who only looks at
> the data.
>
> I have watched a team divide by 60 and ship a dashboard. The number looked
> reasonable. Nobody questioned it for months, because a plausible wrong number is
> far more dangerous than an obviously wrong one.
>
> I do not have a fix to offer yet. I want to sit with the shape of the problem
> first: some questions about your data cannot be answered by your data.

---

## 2. I documented 79 columns for less than a cent

**Visual:** the cost line from the run log, unedited.

> I expected cost to be the interesting part. It was not.
>
> 79 columns across 10 tables, described by an LLM and written back to Unity
> Catalog: 118 seconds, 18,877 tokens, **$0.0048** at list price. Five thousandths
> of a dollar. Extrapolated, a thousand-column catalog runs about four cents.
>
> So the "is it worth the tokens" debate is over before it starts. At these prices
> the question is not whether you can afford to generate descriptions. It is whether
> you can afford to trust them.
>
> One of those generated descriptions told me a coded column value meant
> "after-sale". That meaning does not exist anywhere in the source specification.
> The model invented it, wrote it confidently, and it cost me $0.00005.
>
> Cheap and wrong is not a bargain. A catalog full of confident invented
> descriptions is worse than an empty catalog, because nobody audits what looks
> finished.
>
> I stopped optimising for cost about ten minutes into this project.

---

## 3. What the native AI feature sees, and what it cannot

**Visual:** two panels — the native table comment, and 29 column rows all empty.

> Databricks generates table descriptions with AI. It works, and it is on by
> default. I checked my own workspace before building anything: one table already
> had a fluent, accurate description I never wrote.
>
> Then I looked at its 29 columns. All 29 had no comment at all.
>
> That is not a criticism of the feature. It is a boundary, and the boundary is the
> point. The feature reads the data. So there is a class of question it cannot
> answer — not because it is not smart enough, but because the information is not in
> the data it reads.
>
> A column of integers cannot tell you it is milliseconds. A single letter `F`
> cannot tell you it means "all line items shipped". A field called `pais` holding
> `Brasil` on every row cannot tell you it is a dead legacy column from an
> international launch that was cancelled.
>
> All three of those live in documentation somewhere.
>
> Before you build on top of a native feature, find out what it structurally cannot
> see. That is the only place your work can add anything.

---

## 4. Regex and an LLM agreed on 47 of 47 columns. Then I renamed them

**Visual:** the agreement table, before and after renaming.

> I built two personal-data detectors to compare them: regular expressions on column
> names and values, and an LLM.
>
> On my catalog they agreed on all 47 columns. Seven flagged, forty clear, zero
> disagreements. I had paid a model to confirm what seven regexes already knew.
>
> Then I noticed why. The columns were called `cpf`, `email`, `nome`, `telefone`.
> Both detectors were reading the same signal — the name. My test could not tell them
> apart because the data was too easy.
>
> So I built a table with the same personal data behind `f_01` through `f_06`. Real
> legacy export naming.
>
> The regex found `f_02`, `f_03`, `f_04` — CPF, email, phone all have a detectable
> shape. And it verified the CPF check digits, which is how you tell an 11-digit CPF
> from an 11-digit mobile number. An LLM can only recognise the shape; the regex can
> prove it.
>
> The regex found nothing in `f_01`. It holds names. No regex recognises a person's
> name.
>
> The LLM got `f_01` and `f_05`. It also called a city column a surname — in
> Portuguese, `Oliveira` is both.
>
> They are not competing. They fail in different directions.

---

## 5. My auditor was destroying its own evidence

**Visual:** the three commits, titles only.

> One of the findings is abandoned tables: nothing has read this in 90 days, here is
> what it costs you.
>
> To produce that finding, the tool profiles the table. Profiling is a read. The read
> lands in the same audit log the finding is computed from.
>
> Run it twice and every abandoned table looks actively used. The auditor was
> contaminating the evidence it was collecting.
>
> Attempt one: record each scan's start and end time, subtract that window from the
> log. Failed — Unity Catalog writes audit events asynchronously, so my scan's reads
> were stamped 70 seconds after the window closed and read back as real traffic.
>
> Attempt two: pad the window by 20 minutes each side. Fixed that, and swallowed a
> genuine analyst session that ran four minutes after a scan. Every scan was now
> creating a 40-minute blind spot.
>
> Attempt three: stop using time. Tag every statement with a marker comment and
> exclude statements carrying it. Identity, not timing.
>
> Two failed attempts are in the git history with the reasoning. That is the part
> worth keeping.

---

## 6. I rigged my own experiment, and it took me a day to notice

**Visual:** side by side — the rigged result, the honest result.

> I built a feature that feeds source-system documentation to a documentation agent,
> so it can know things the data cannot tell it.
>
> To prove it worked, I wrote documentation for a source system, generated tables to
> match, ran it, and got a clean sweep. Every description improved. I nearly
> published that.
>
> Then: I wrote the documentation. I generated the data. I declared the result. That
> test could not fail. And a test that cannot fail proves nothing — it only proves
> text I put into a prompt came back out.
>
> So I ran it again against material I had not touched: the TPC-H specification from
> tpc.org, and the `samples.tpch` tables that ship with Databricks. 61 columns, 30
> million rows.
>
> Grounding changed 56 descriptions. About **6** changed meaningfully. The rest was
> rewording, which is just what language models do — "changed" is close to a useless
> metric.
>
> Six out of 61 is a real result. The clean sweep was a mirror.
>
> If your demo cannot fail, you have not tested anything yet.

---

## 7. The model had the answer in its prompt and ignored it

**Visual:** the retrieved passage next to the description it produced.

> Three columns in TPC-H hold single letters: `N`, `A`, `R`, `O`, `F`, `P`. The
> specification states exactly how each is derived — `O` if the ship date is in the
> future, `F` otherwise.
>
> I gave the agent that specification. It wrote "O for open, F for final" and moved
> on.
>
> My first assumption was a retrieval failure — wrong passage, missed chunk. So I
> checked what had actually been sent, because every description records the passages
> it was grounded in. The correct passage was retrieved. The rule was in the prompt.
> The model read it and preferred its own familiar guess about a famous benchmark.
>
> The fix was not better retrieval. It was one paragraph: carry the rule the
> documentation states, and know that repeating what a reader already assumed adds
> nothing.
>
> All three columns now state the actual condition.
>
> Everyone building RAG assumes the retriever is the weak link. Sometimes you have
> already delivered the answer and the model is talking over you. You only find out
> if you log what you sent.
>
> Catalog health went 34 → 89. Code and the full write-up: [repo link]

---

## Notes on running the series

Do not defend the project in the comments. Post 6 will attract "so it barely
works?" — the answer is that six real improvements on unfamiliar data is a result
and a clean sweep on data you authored is not, and then stop.

Post 4 and post 7 are the two most likely to travel. Post 4 because the CPF check
digit detail is concrete and unfamiliar to a non-Brazilian audience. Post 7 because
"the retriever was fine, the model ignored me" contradicts what most people building
RAG assume.

Post 1 asks for nothing and offers no solution. Resist adding one.
