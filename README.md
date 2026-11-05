# Sales Desk

One robot answers a shop's WhatsApp, Instagram, TikTok and email. It:
- remembers each customer;
- quotes exact prices from the price list;
- follows up when a customer goes quiet;
- reads the shop's bank alert emails to confirm payments;
- hands discounts, bulk orders and complaints to the owner.

The owner starts each day on a **morning brief**, and sees every chat as an order on the **order board**.

Built 2026-11-04 for the Meta Global AI Developer Hackathon, by urelimitless. The demo shop, "Nkem Fabrics, Enugu", is made up.

## What it does

- **One inbox for four apps.** WhatsApp (Cloud API) and Instagram DMs come in by webhook. Email arrives from a forwarder. TikTok chats are forwarded by a tool such as ManyChat or Make, because TikTok's messaging API is invite-only, so TikTok replies wait for the owner to send them in the app.
- **Prices nobody can get wrong.** The robot picks the product and quantity. The desk works out the subtotal, the delivery fee for the customer's area and the total from `products.csv` and `shop.json`. Any naira amount in a reply that isn't in the shop's facts is blocked, and the chat goes to the owner.
- **Selling, step by step:**
  1. They ask, and the robot answers.
  2. They say what they want, and the robot quotes.
  3. They say "yes", and the robot sends the account details with an order code (NF-12), asking them to put it in the transfer description.
  4. They say they paid, and the order waits for proof.
- **Memory.** Name, delivery area, what they like, event dates and notes, so returning customers never repeat themselves.
- **Follow-ups.** After 20 quiet hours, at most twice. The owner can skip today's follow-up from the brief.
- **Hand-overs.** These always go to the owner: discounts, "last price", bulk orders (over 20 yards), complaints, stock shortfalls, and anything the robot isn't sure about. When the owner replies from the board or from Telegram, they take over the chat and the robot stays quiet until they give it back.
- **STOP** opts anyone out.

## Payments from bank alert emails

The robot reads the shop's credit alert emails and matches each one to an order waiting for money:
- the amount must be exact, plus
- the order code in the transfer description, or the sender's name.

**It confirms by itself only when all of these hold:**
1. The alert came from a domain on `BANK_ALERT_SENDERS`.
2. The receiving mail server recorded `dkim=pass` or `dmarc=pass` for that domain. Fake credit alerts can't pass this.
3. The alert matches exactly one order, and either the order code or name matched, or the customer already said they paid.

Then the customer gets "Payment received" and the order moves to **Paid, to send**. Everything else waits on the owner's brief with one click:
- an alert that isn't signed;
- two orders with the same amount;
- an amount-only match before the customer says they paid.

Money that matches no order is listed as **Money in, no order**. `AUTO_CONFIRM=false` makes the robot always ask.

There are two ways to connect it:
- **IMAP (simplest).** Set `IMAP_HOST=imap.gmail.com`, `IMAP_USER`, and `IMAP_PASSWORD` to a Gmail **app password**. The robot reads the mailbox every 2 minutes, read-only: nothing is marked, moved or deleted.
- **Forwarder.** Post each raw alert email, headers included, to `/inbound/bank-alert?token=<INBOUND_TOKEN>`. A Cloudflare Email Worker can do this.

Before going live:
1. Open one real alert email from the shop's bank and note its From domain. Set `BANK_ALERT_SENDERS` to exactly that.
2. Save that email as `.eml` and run `python -m salesdesk parse-alert alert.eml` to see the amount, sender, description and whether it's verified.

The defaults in `config.py` are guesses to check, not facts.

## Run it

```bash
cd sales-desk
pip install -r requirements-dev.txt
copy .env.example .env      # then fill in .env
python -m pytest -q         # 45 offline tests
python -m salesdesk serve   # http://127.0.0.1:8792/
```

The screens:
- `/` is the morning brief.
- `/board` is the order board; `?c=<customer id>` opens a chat.
- `/shelf` is the shelf camera: take a snapshot (the demo has two sample frames) or upload a photo.
- `/phone` is the demo phone: chat as a customer on any channel, send a "receipt photo", fire a test bank alert, or reset the demo.

Other commands:

```bash
python -m salesdesk chat --channel instagram   # be a customer in the terminal
python -m salesdesk brief                      # print the morning brief
python -m salesdesk alerts --days 2            # read the mailbox once and match alerts
python -m salesdesk parse-alert alert.eml      # check how one bank email is read
python -m salesdesk seed                       # reset to the demo shop's sample morning
```

`PROVIDER` picks the model:
- `claude` or `muse` (Meta's Muse Spark) is for real shops.
- `gemini` is free-tier testing only. Google may use free-tier prompts, so never use it with real customers' messages.
- `rules` needs no key.

## Meta: Muse models and your ads (built 4 Nov 2026 for the Meta Global AI Developer Hackathon)

Set `META_API_KEY` (a Meta Model API key from dev.meta.ai) and `PROVIDER=muse`. Then:

| Meta model | What it does here |
|---|---|
| **Muse Spark 1.3** | The robot's brain: replies, quotes, hand-overs (JSON schema output, `salesdesk/brain.py`). |
| **Muse Spark, reading images** | Reads transfer screenshots: amount, sender, description, anything that looks edited. It only helps the owner check: a screenshot never confirms a payment (`salesdesk/senses.py`). |
| **Muse Voice Transcribe** | WhatsApp voice notes become text the robot answers. Notes are converted to 16 kHz WAV with ffmpeg first. |
| **Muse Spark, advising** | The "Your ads yesterday" card: for each ad, what's happening in the customers' own words and one thing to try. Any naira amount it writes that isn't in the numbers or the price list is thrown out (`salesdesk/ads.py`). |
| **Muse Image** | Drafts a new picture for a money-losing ad. Sales Desk prints the real price on it from the price list (image models garble text). It is saved for the owner to look at, never posted. |
| **Ask Sales Desk** | The mic on the brief. Muse Voice Transcribe hears the owner's question; Muse Spark answers in at most 3 sentences from the brief, the ad report and the price list only, and the browser reads it aloud. An answer with a naira amount that isn't in those facts is replaced by the brief's headline (`salesdesk/ask.py`). |
| **SAM 3.1** | The shelf camera (`/shelf`). Each snapshot is counted with one phrase ("stack of folded fabric on the shelf") through `POST /v1/responses`. Specks and anything cut off by the frame edge are dropped. When the count falls, the desk compares it with paid orders for that shelf since the last snapshot and shows what left the shelf unpaid (`salesdesk/shelf.py`). |

**Which ad made money.** When someone taps a click-to-WhatsApp or click-to-Instagram ad, the first message carries the ad's
ID (the webhook's `referral`). Each customer keeps the ad that brought them, and a sale counts for that ad only once the
money is confirmed. Spend comes from the Marketing API (read-only Insights, `GET /act_<id>/insights?level=ad`) when
`META_ADS_TOKEN` (with `ads_read`) and `META_AD_ACCOUNT` are set; otherwise from the shop's `ads.json`, shown as a sample.

**Never automatic:** nothing here changes an ad, its budget, or posts anything. The owner decides.

**Later (not built):**
- Send confirmed sales back to Meta with the Conversions API for Business Messaging (the saved `ctwa_clid`), so Meta shows the ads to people who buy, not just people who chat.
- Shelf camera on a live CCTV feed: today it counts single snapshots (the demo's two frames are made-up sample photos), keeps them in memory only, and doesn't change the stock in `products.csv`.

## Set up a real shop

1. Copy `demo/` and edit its three files:
   - `shop.json`: the name, owner, payment account, delivery zones and fees, the bulk limit and follow-up timing.
   - `products.csv`: price per unit, stock, and words customers use.
   - `faq.md`.
2. Set `SHOP_DIR` to that folder, and set `DEMO_TOOLS=false`.
3. Connect the channels:
   - **WhatsApp:** webhook `/webhook/whatsapp`, plus an approved template for follow-ups after 24 hours.
   - **Instagram:** webhook `/webhook/instagram`.
   - **Email:** a forwarder posting to `/inbound/email?token=...`, and SMTP for replies.
   - **TikTok:** a forwarder posting `{sender, name, text}` to `/inbound/tiktok?token=...`.
   - **Telegram:** a bot and the owner's group, for alerts and `/brief`.
4. Connect the bank alerts (see above).

## Honest limits

- Run live here (4 Nov) with a real Meta key: Muse Spark chat, receipt reading, voice-note transcription, ad advice and
  Muse Image drafts. The ad spend in the demo is the `ads.json` sample; the Marketing API call is tested only against a
  mock, not a real ad account.
- Only run here, with the demo phone and test alerts: the rules brain and live Gemini. **Not run live yet:**
  - Claude;
  - WhatsApp, Instagram, email (inbound or SMTP), TikTok forwarding and Telegram;
  - IMAP against a real Gmail account;
  - a real bank's alert email.
- The bank alert reader handles the common "Amount / Sender / Narration" layouts, in plain text or HTML tables. Some banks word their alerts differently: check one real alert first.
- Stock goes down when a payment is confirmed, but only in memory. `products.csv` stays the source, so update it when stock changes.
- Everyone who opens the screens is "the owner". There are no staff logins yet. Put it behind `DEMO_KEY` or a private network.
- Nigeria's data protection law (NDPA 2023) applies to customers' chats. Tell the shop what is stored (chats, names, areas, payments) and use `KEEP_DAYS`.
