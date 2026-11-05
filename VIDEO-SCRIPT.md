# Video script: Sales Desk Pay (target 2:40, hard limit 3:00)

Public on YouTube. Show it working. Record the screen at 1080p with the board and the phone side by side (browser windows, 50% each). Use the **real PayPal sandbox** if the keys are in; if you are on the mock, say "mock PayPal" out loud in shot 5 and keep the **MOCK PAYPAL** badge in view. Never show a Client ID, a Secret, a Webhook ID or the sandbox password.

Before recording: `python -m salesdesk seed`, then start the server. Have `/board` and `/phone` open.

| # | Time | On screen | Say |
|---|---|---|---|
| 1 | 0:00 | Board, the Zuri Cloth rows | "Zuri Cloth is a made-up African fabric shop in Peckham. Its sales robot talks to customers on WhatsApp and Instagram. This is Sales Desk Pay. It sends PayPal links, and it only believes PayPal." |
| 2 | 0:15 | Phone: ask for 6 yards of blue Ankara, say Hackney, say yes | "A customer asks for fabric. Prices come from the price list, never from the AI. She says yes." |
| 3 | 0:35 | Board: the new row says **Needs your OK**. Press **Approve link** | "The robot does not make a payment link by itself. The owner approves first. Now it creates a PayPal order and sends the link." |
| 4 | 0:55 | Phone: send the fake PayPal receipt photo, then type "I've paid". Board row stays **Awaiting PayPal**. Click the row to show the robot's answer | "Now someone cheats. A fake PayPal receipt, perfect amount, perfect order code. And 'I've paid'. The robot says it can only confirm when PayPal tells it. The row stays Awaiting." |
| 5 | 1:20 | Phone: **Send forged PayPal message**, pick "Real £1 message edited to the full price". Read the result line | "Next, a forged PayPal message. Real signature for one pound, edited to say seventy-six. The shop asks PayPal to verify it. PayPal says no, we answer 401, nothing changes." |
| 6 | 1:45 | Click the payment link in the phone chat. Pay as the sandbox buyer. Come back to the board | "Now the real payment. The buyer pays on PayPal." |
| 7 | 2:00 | Board: the row flips to **Paid**, the events grid shows PAYMENT.CAPTURE.COMPLETED. Phone shows "Payment received through PayPal" | "PayPal sends a signed webhook. The shop verifies it with PayPal, checks the amount, the currency and the order, and only then marks it Paid. The buyer gets the message." |
| 8 | 2:15 | Terminal: `python -m salesdesk forged-eval`, the last lines | "I tried thirty forgeries: words, screenshots where the reader is fully fooled, and fake webhooks. Zero confirmed. A real payment still goes through." |
| 9 | 2:30 | The README table or the code line `Desk.paypal_event` | "Orders, capture and webhook verification are PayPal's APIs. The board is AG Grid. One function can mark an order Paid, and a test proves it." |
| 10 | 2:40 | End card: repo address | "Sales Desk Pay. Open source, MIT." |

Tips
- If a line runs long, cut shot 9. Shots 4, 5, 7 and 8 are the ones that win.
- Don't read the table aloud. Say each line in your own words.
- Upload as **Public**. Put the repo link in the description. Don't put keys on screen.
