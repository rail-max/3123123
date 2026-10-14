# Platega and legal documents setup

After Railway deploys this version, add these variables to the `worker` service:

```text
PLATEGA_MERCHANT_ID=<MerchantId from Platega>
PLATEGA_SECRET=<API key from Platega>
PUBLIC_BASE_URL=https://<your-public-railway-domain>
PROJECT_NAME=DialogDelBot
```

The public documents name the bot's built-in support ticket system as the
support channel.

You can host the legal pages on Telegraph. Add both variables if you want the
bot buttons to use Telegraph instead of the Railway fallback pages:

```text
PRIVACY_POLICY_URL=https://telegra.ph/your-privacy-policy
TERMS_URL=https://telegra.ph/your-terms
```

In Platega, open Settings -> Callback URLs and set:

```text
https://<your-public-railway-domain>/platega-webhook
```

The callback must use the public Railway HTTPS domain, never a private Railway
database/service address. Platega sends `X-MerchantId` and `X-Secret`; the bot
checks both, checks the expected amount and currency, and grants access once.

Public pages that must be available for bank review:

```text
https://<your-public-railway-domain>/privacy-policy
https://<your-public-railway-domain>/terms
https://<your-public-railway-domain>/tariffs
```

The tariff is 30 days for 120 RUB through SBP / QR (paymentMethod 2) or
cryptocurrency (paymentMethod 13). Enable crypto in the same Platega merchant
account; no additional bot secrets or webhook endpoint are needed. Platega
displays the crypto amount, supported networks and any applicable fees.
Check the provider's minimum payment amount before offering this tariff.

The bot uses POST /transaction/process without a client-generated ID. It stores
the returned transactionId before sending the payment link. Crypto invoices
use plan platega_crypto_monthly; their RUB invoice value is included in Platega
totals, not recorded as a USDT balance. Confirmation and refund handling are
shared with SBP and remain idempotent. Existing invoices keep their IDs.

Telegram Stars tariffs remain available: 1 day for 35 Stars, 7 days for
45 Stars, 30 days for 100 Stars, and 365 days for 850 Stars.

Telegram requires Stars exclusively for digital goods/services sold inside
bots. Keeping Stars alongside Platega does not remove this restriction:
https://core.telegram.org/bots/payments-stars#faq

After deployment, check one crypto invoice, its authenticated CONFIRMED
callback, subscription grant, locked-message delivery and payment history.
Repeat the callback to verify it cannot grant a second subscription. Local
tests use mocked provider responses and do not prove live merchant availability.
