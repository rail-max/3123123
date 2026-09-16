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

The current ruble tariff is 30 days for 120 RUB through SBP / QR. Telegram
Stars tariffs remain available in the bot: 7 days for 45 Stars, 30 days for
100 Stars, and 365 days for 850 Stars.
