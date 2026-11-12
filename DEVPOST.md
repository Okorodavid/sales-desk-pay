# Devpost text (paste into the form; the owner submits)

## Project name
Sales Desk Pay

## Elevator pitch (short field)
A sales robot that sends PayPal links and only marks an order Paid when PayPal's own signed webhook says so. Screenshots and "I've paid" don't count.

## Description

**The problem.** Small online sellers close sales in chat apps. The buyer sends a screenshot and says "I've paid". Screenshots are easy to fake, so sellers either wait and lose the sale, or ship and lose the goods. A sales robot that accepts a screenshot is worse: it can be talked into shipping for free.

**What it does.** Sales Desk Pay answers customers on WhatsApp, Instagram, TikTok and email, quotes exact prices from the shop's own price list, and turns a "yes" into a PayPal payment link. The shop owner approves each order first. Then the robot creates a PayPal order and sends the link. The order becomes Paid only when PayPal sends a signed `PAYMENT.CAPTURE.COMPLETED` webhook that the shop has verified through PayPal's verify-webhook-signature API, and the amount, currency, order and status all match. The robot then tells the buyer.

**How PayPal is central.** Orders v2 makes the payment link (intent CAPTURE, the shop's order code as `custom_id`, an idempotency key so a double click makes one order). Capture collects the money when the buyer returns. Webhook verification is the only door money comes through. No other code path can mark an order Paid, and a test proves it against the source.

**The AI part.** The robot's brain (Claude, Gemini or Meta's Muse Spark) reads the chat and proposes one action: quote, accept, hand over to the owner. It proposes only. The desk checks every action. Prices and totals come from the price list. The model has no "confirm payment" action, so a clever message can't create one. Receipt photos are read by a vision model to help the owner, but are never proof.

**Evaluation.** Thirty forged attempts: ten "I've paid" messages and prompt-injection attempts, ten fake PayPal receipt screenshots read by a reader that is fooled completely, and ten forged webhooks (no signature, made-up signature, wrong key, edited amount, edited order, and genuinely signed messages that are for the wrong amount, wrong currency, pending, only approved, or for an unknown order). **0 of 30 confirmed an order.** A real payment still confirms (the control). Run it with `python -m salesdesk forged-eval`.

**Design.** The owner's morning brief puts what needs a decision first, usually an order waiting for approval. The order board is AG Grid Community: every order is a row, the Stage and "PayPal says" columns flash when PayPal's message arrives and the row flips from Awaiting PayPal to Paid. A second grid lists every PayPal event the shop received and what it did with it.

**Potential impact.** Chat selling is how millions of small sellers work, including the diaspora sellers who ship fabric, food and beauty products to customers in the UK, the US and Canada. Fake payment screenshots are a daily loss for them. The same rule works for any agent that handles money: the agent can ask, a person approves, and only the payment provider can say the money arrived.

**What was built in the contest window.** Sales Desk (github.com/Okorodavid/sales-desk, MIT) was built on 27 Oct to 4 Nov 2026 for another hackathon. Sales Desk Pay starts from a copy of it on 5 Nov. The first git commit is that copy. Everything after it is new for this contest: the PayPal client, the webhook verifier and event checks, the owner-approval gate, the mock PayPal, the forged-receipt evaluation and forgery kit, the AG Grid board, the GBP demo shop. The README has a table of what was copied, what changed and what was removed.

**Honest limits.** Wallet payments only. No refunds or disputes yet. The offline demo runs on a built-in mock PayPal (clearly labelled MOCK); an actual sandbox payment passed end to end on 12 Nov: GBP 28, verified completed-capture webhook, Paid status and buyer confirmation. The video still needs recording.

## Built with
Python, aiohttp, SQLite, PayPal REST APIs (Orders v2, Webhooks verify), AG Grid Community, Claude / Gemini / Muse Spark, Pillow.

## Try it
Repo: https://github.com/Okorodavid/sales-desk-pay. Run steps and the sandbox buyer login are in the README. Hosted demo: (a Render link, if the owner sets one up).

## Sponsor prizes
AG Grid: AG Grid Community is used for both grids (the order board and the PayPal events log). A project can win one main prize or honourable mention plus one sponsor prize; the other sponsors' tools (APIMatic, Bryntum, Channel3, Render) are not used, so don't tick them. Render is only a deploy option in `render.yaml`.

## Honourable mention to aim for
Best Use of Agentic Commerce.
