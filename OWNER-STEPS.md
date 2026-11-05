# Owner steps (do step 1 first, it takes 10 minutes)

Deadline: Thu 12 Nov, 21:00 Lagos. Claude never creates accounts or types keys. You do these.

## 1. The PayPal sandbox check (tonight)

1. Open developer.paypal.com and log in with your PayPal account.
2. Make sure the switch at the top says **Sandbox** (not Live).
3. Go to **Apps & Credentials** and press **Create App**.
   - Name: `sales-desk-pay`.
   - Type: **Merchant**. Do **not** pick "Platform" (it is limited to a few countries).
4. If the app is created, you will see a **Client ID** and a **Secret**. Copy them into a new file `sales-desk-pay\.env` (copy `.env.example` first):

   ```
   PAYPAL_CLIENT_ID=paste here
   PAYPAL_SECRET=paste here
   ```

   Paste them into the file only. Do not paste them into the chat.
5. Go to **Testing tools > Sandbox accounts**. PayPal makes one business account (the shop) and one personal account (the buyer). Open the personal one (**...** > **View/Edit account**) and copy its email and password into a note. The README needs them so judges can pay.
6. **If step 3 fails** (an error, a country block, or no Create App button): stop and tell Claude exactly what the page says. You then decide whether to skip this contest. The build carries on with a mock PayPal meanwhile.

## 2. The webhook (after step 1 works)

PayPal can only send a payment message to a public https address.

1. Start the tunnel yourself in a terminal: `cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8792`. It prints an address like `https://something.trycloudflare.com`.
2. On the app's page in developer.paypal.com, scroll to **Webhooks** and press **Add Webhook**.
   - URL: the tunnel address plus `/paypal/webhook`.
   - Events: tick **Payment capture completed** (and, if you like, **Payment capture denied**).
3. Copy the **Webhook ID** it shows into `.env`: `PAYPAL_WEBHOOK_ID=paste here`.
4. Set `PUBLIC_URL=` in `.env` to the tunnel address (no slash at the end). The address changes each time the tunnel restarts, so repeat step 2 then.

## 3. Optional

- **AG Grid:** nothing to sign up for. The free Community version is used.
- **Render:** only if you want a hosted demo link. Tell Claude when you have an account; the setup file is ready.
- **Devpost sponsor rules:** Devpost blocks Claude from reading its pages (error 403). Open `paypalaihackathon.devpost.com/rules` yourself and paste the AG Grid and Render sections to Claude, so nothing is claimed that the rules don't allow.

## 4. At the end

- Upload the video to YouTube as **Public** (Claude writes the script in `VIDEO-SCRIPT.md`).
- Push the repo to GitHub as public (Claude asks you first), and press Submit on Devpost. The text is in `DEVPOST.md`.
