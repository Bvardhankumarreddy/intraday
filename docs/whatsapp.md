# WhatsApp alerts (Meta WhatsApp Cloud API)

The alerts describe the **PAPER** system only. They are research signals, not
investment advice. Nothing here can place an order: you decide, and you place
any real order manually. Send alerts only to **your own number**. Sending
stock buy or sell calls to other people needs SEBI registration.

## What you receive

| Message | When | Example |
|---|---|---|
| `PAPER SIGNAL` | the AI-assisted arm records a CANDIDATE (every deterministic check passed and the AI did not object) | `PAPER SIGNAL SYNTH at 09:40: entry above Rs 71.0, stop Rs 70.4, target Rs 72.2, qty 28 (whole shares), est. costs Rs 6.03, net reward:risk 1.6, valid till 09:50. ...` |
| `PAPER ENTRY` / `PAPER EXIT` | a paper position fills or closes (baseline and AI-assisted arms) | `PAPER EXIT (AI-assisted) SYNTH: sold 28 @ Rs 72.2 at 09:50 (TARGET); net P&L Rs 15.69 after Rs 17.91 costs` |
| `SYSTEM CRITICAL` / `RESOLVED` | monitoring alerts at or above `min_alert_severity`, and their recovery | `SYSTEM CRITICAL: market-data - no symbol has fresh, valid completed candles ...` |
| `PAPER DAILY SUMMARY` | 16:00 (post-session job) | both arms' signals, trades, net P&L and cash |

Timing: a signal fires at a 5-minute candle close, and the paper entry
assumes the **next candle's open**, about a minute later. WhatsApp delivery
plus manual entry will often give a different price from the paper result.

## One-time Meta setup

1. **Create the app.** In developers.facebook.com, go to My Apps → Create app
   → type **Business**, then add the product **WhatsApp**.
2. **Sender number.** WhatsApp → *API Setup* shows a free **test phone
   number** and its **Phone number ID**. Under "To", add **your own WhatsApp
   number** and verify it with the code you receive. Test numbers can message
   only verified recipients, which is all this needs.
3. **Permanent token.** In business.facebook.com, go to Settings → Users →
   **System users** → add one (Admin). Under *Assign assets*, add the app and
   the WhatsApp account. Then *Generate token* with
   `whatsapp_business_messaging` (and `whatsapp_business_management`), set to
   never expire. The 24-hour token on the API Setup page is only for a quick
   test.
4. **Message template.** In WhatsApp Manager → Message templates → Create,
   enter:
   - Category: **Utility**
   - Name: **`nse_alert`**
   - Language: **English** (`en`)
   - Body (one variable, not at the start or end):
     `NSE paper-trading alert: {{1}}. Research only, not investment advice.`
   - Sample for {{1}}: `PAPER SIGNAL SBIN at 09:40: entry above Rs 80.1, stop Rs 79.5`

   Submit it. Approval usually takes minutes to 24 hours. Business-initiated
   messages are rejected until the template is **Approved**.
5. **Pricing.** Check Meta's current WhatsApp pricing page. Charges depend on
   the template category and country, and they change. The
   `max_messages_per_day` cap (default 60) limits spend.

## Configure

Add these to `.env` (secret; never commit it):

```
NSE_WHATSAPP_TOKEN=<permanent system-user token>
NSE_WHATSAPP__ENABLED=true
NSE_WHATSAPP__PHONE_NUMBER_ID=<Phone number ID from API Setup>
NSE_WHATSAPP__RECIPIENTS=["91XXXXXXXXXX"]
```

Optionally set `NSE_WHATSAPP__API_VERSION`, using the Graph API version shown
in your app (default `v23.0`). Message choices live in the `whatsapp:`
settings section.

Test locally:

```bash
python -m app.notify check      # READY / what's missing (sends nothing)
python -m app.notify test       # sends ONE test message through the template
python -m app.notify preview    # today's messages (sends nothing)
python -m app.notify log        # what was sent: ACCEPTED / FAILED / SKIPPED
```

"ACCEPTED" means Meta accepted the request. It does not confirm that the
phone received it (delivery receipts would need a webhook, which is not set
up).

**On the k3s server:** update the Secret from the new `.env` and restart the
dashboard (see `docs/deployment.md`, "Rotate secrets"). The next session and
post-session pods pick it up automatically.

## Safeguards

- WhatsApp settings are excluded from the configuration version, so enabling
  them does not re-version the running evaluation.
- Each event is sent once (a keyed, append-only log), with a daily cap.
- Bounded retries apply only to timeouts, 429 and 5xx. Token, template and
  number errors are logged and not retried.
- A failed notification never affects trading. Numbers are masked and the
  token is redacted in all records.
