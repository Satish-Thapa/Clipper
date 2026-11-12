# Connecting your socials

How to connect **YouTube, Facebook, Instagram, TikTok** and the **WhatsApp report** to Clipper.

Each one is optional. Set up only the ones you want. Every key goes in the `.env` file in the project folder, and
then you **restart the server**. The **Socials** page shows a green dot for each platform that's connected.

| Platform | What you need | Time | Goes in `.env` |
|---|---|---|---|
| YouTube | `client_secret.json` + click Connect | 10 min | nothing |
| Facebook | A Facebook **Page** + a Meta developer app | 20 min | `FB_PAGE_ID`, `FB_PAGE_TOKEN` |
| Instagram | A **Business/Creator** Instagram linked to that Page | +5 min | `IG_USER_ID` |
| TikTok | A TikTok developer app | 30 min + review | `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_REFRESH_TOKEN` |
| WhatsApp report | The Meta app from Facebook + your phone | 20 min + template approval | `WA_PHONE_NUMBER_ID`, `WA_TOKEN`, `WA_TO`, `WA_TEMPLATE` |

> Developer portals change their menus often. If a button has a slightly different name, look for the closest match.

---

## YouTube

YouTube is covered in the [README](README.md#youtube-upload-setup-one-time): `client_secret.json`, then **Connect YouTube**
on the Socials page.

**Already connected before the WhatsApp report existed?** Click **Reconnect YouTube** once. The old login could only
upload; the new one can also read view counts.

---

## Facebook (Page)

Clipper posts to a **Facebook Page**, not a personal profile. If you don't have one: facebook.com → **Pages** → **Create new Page**.

### 1. Create a Meta app
1. Go to [developers.facebook.com](https://developers.facebook.com/) → **My Apps** → **Create App**.
2. Choose the use case **Other**, then app type **Business**. Give it a name (e.g. "Clipper").
3. In the app dashboard, add the products **Facebook Login for Business** and **Instagram Graph API** (the second one
   is for Instagram; add it now even if you only want Facebook).

You can leave the app in **Development** mode. As the app's admin you can post to your own Page.

### 2. Get a token with the right permissions
1. Open the [Graph API Explorer](https://developers.facebook.com/tools/explorer/).
2. Top right: pick **your app**, and under **User or Page** choose **User Token**.
3. Click **Add a permission** and tick all of these:

   | Permission | Why |
   |---|---|
   | `pages_show_list` | find your Page |
   | `pages_read_engagement` | read the Page |
   | `pages_manage_posts` | post Reels / videos |
   | `read_insights` | view counts for the WhatsApp report |
   | `instagram_basic` | Instagram (skip if no Instagram) |
   | `instagram_content_publish` | post Instagram Reels |
   | `instagram_manage_insights` | Instagram view counts |
   | `business_management` | linked business accounts |

4. Click **Generate Access Token** and accept everything in the popup (choose your Page and Instagram account when asked).

**A permission isn't in the list (e.g. `pages_manage_posts`)?** The Explorer only offers permissions your app has
been given. Fix it in the app dashboard (developers.facebook.com → your app), then reload the Explorer:
- **The left menu has "Use cases"** (newer apps): **Use cases** → **Add use case** → **Manage everything on your Page**
  → then **Customize** on it and click **Add** next to `pages_manage_posts`, `pages_read_engagement`,
  `pages_show_list`, `read_insights`. For Instagram also add the use case **Manage messaging & content on Instagram**
  (with `instagram_basic`, `instagram_content_publish`, `instagram_manage_insights`).
- **The left menu has "App Review → Permissions and features"** (older/Business apps): find `pages_manage_posts`
  there and click **Get** / **Request** for Standard Access. You don't need App Review to post to your own Page.
- **No Page use case is offered at all:** the app was created with a type that has no Page access (e.g. only
  "Authenticate with Facebook Login"). Create a new app and choose the Page use case, or **Other → Business**.
- In the Explorer, `pages_*` permissions are under the **Pages** group; typing the name in the search box also works.

### Shortcut: let the script do steps 3-4 and the Instagram ID
With the token from step 2 still on screen, run this in the project folder:
```powershell
.venv\Scripts\python -m clipper.meta_setup
```
It asks for your **App ID** and **App Secret** (developers.facebook.com → your app → **App settings → Basic**), and for
the **User Token** from Graph API Explorer. These go in hidden prompts. It then checks the permissions, makes the token
long-lived, finds your Page and its never-expiring Page token, finds the linked Instagram account, and writes
`FB_PAGE_ID`, `FB_PAGE_TOKEN` and `IG_USER_ID` into `.env`. If that works, skip to restarting the server.

### 3. Make it long-lived
The token from the Explorer dies in about an hour.
1. Click the blue **ⓘ** next to the token → **Open in Access Token Tool**.
2. Click **Extend Access Token** at the bottom and copy the new (long) token.

### 4. Get the Page ID and Page token
1. Back in the Graph API Explorer, paste the long token into the **Access Token** box.
2. Run this query (GET): `me/accounts`
3. In the answer, find your Page:
   ```json
   { "name": "NAH THAT'S AI", "id": "1234567890", "access_token": "EAAB..." }
   ```
4. Put them in `.env`:
   ```
   FB_PAGE_ID=1234567890
   FB_PAGE_TOKEN=EAAB...      (the access_token from me/accounts, NOT the user token)
   ```
   A Page token made from a long-lived user token **doesn't expire**.

Clips up to 90 s go up as **Reels**; longer ones as normal Page videos.

---

## Instagram

Uses the same Meta app and the same token as Facebook, so do the Facebook part first.

### 1. Make your Instagram a professional account, linked to the Page
1. Instagram app → **Settings** → **Account type and tools** → **Switch to professional account** (Business or Creator).
2. Link it to your Facebook Page: on Facebook, open the Page → **Settings** → **Linked accounts** → **Instagram** → connect.

### 2. Get the Instagram account ID
In the Graph API Explorer (with the long token from Facebook step 3), run:

```
<FB_PAGE_ID>?fields=instagram_business_account
```

Answer:
```json
{ "instagram_business_account": { "id": "17841400000000000" }, "id": "1234567890" }
```

Put it in `.env`:
```
IG_USER_ID=17841400000000000
```

Instagram uses `FB_PAGE_TOKEN` automatically. Only set `IG_TOKEN=` if you want a different token for Instagram.

Every clip is posted as a **Reel**. Instagram limits API posting to about 50 posts a day.

---

## TikTok

TikTok is the slowest to set up because the app needs TikTok's approval before posts can be **public**. Until then,
everything Clipper posts is **private (only you can see it)**, and you make it public in the TikTok app.

### 1. Create a TikTok developer app
1. Go to [developers.tiktok.com](https://developers.tiktok.com/) and log in with your TikTok account.
2. **Manage apps** → **Connect an app** (or **Create app**).
3. Fill in the basics (name, icon, category, description). Terms/privacy URLs can be any page you own.
4. **Add products**: **Login Kit** and **Content Posting API**. In Content Posting API, turn on **Direct Post**.
5. **Scopes**: add `user.info.basic`, `video.upload`, `video.publish`.
6. **Login Kit → Redirect URI**: add an https address, e.g. `https://example.com/callback`. Nothing needs to run there;
   you only need to copy the code from the browser's address bar later.
7. Copy the **Client key** and **Client secret** into `.env`:
   ```
   TIKTOK_CLIENT_KEY=aw...
   TIKTOK_CLIENT_SECRET=...
   ```

To test before approval, use the app's **Sandbox** and add your TikTok account under **Target users**.

### 2. Log in once to get a refresh token
1. Open this in your browser (replace `YOUR_CLIENT_KEY`; the redirect must match step 6 exactly):
   ```
   https://www.tiktok.com/v2/auth/authorize/?client_key=YOUR_CLIENT_KEY&scope=user.info.basic,video.upload,video.publish&response_type=code&redirect_uri=https%3A%2F%2Fexample.com%2Fcallback&state=clipper
   ```
2. Log in and click **Authorize**. You land on `https://example.com/callback?code=XXXX&...`. The page itself may show
   an error; that's fine. Copy the value after `code=` up to the next `&`.
3. In **PowerShell**, in the project folder (fill in the three values):
   ```powershell
   $body = @{ client_key = "YOUR_CLIENT_KEY"; client_secret = "YOUR_CLIENT_SECRET";
              code = [uri]::UnescapeDataString("PASTE_CODE_HERE"); grant_type = "authorization_code";
              redirect_uri = "https://example.com/callback" }
   Invoke-RestMethod -Method Post -Uri "https://open.tiktokapis.com/v2/oauth/token/" -Body $body `
     -ContentType "application/x-www-form-urlencoded"
   ```
   The code expires within minutes, so run this right away.
4. Copy `refresh_token` from the answer into `.env`:
   ```
   TIKTOK_REFRESH_TOKEN=rft.xxxxxx
   ```

Clipper keeps the token fresh by itself (it saves the newest one in `data/tiktok_token.json`). The refresh token
lasts about a year. If TikTok uploads start failing with a token error, repeat step 2.

### 3. After TikTok approves the app
Submit the app for review in the portal. Once it's approved, set:
```
TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE
```

TikTok doesn't report view counts for private posts, so the WhatsApp report shows TikTok as unavailable until then.

---

## WhatsApp report (hourly views)

Sent with Meta's official **WhatsApp Cloud API**, from the same Meta app you made for Facebook. Do the Facebook
"Create a Meta app" step first.

### 1. Add WhatsApp to your Meta app
1. [developers.facebook.com](https://developers.facebook.com/) → **My Apps** → your app → **Add product** → **WhatsApp** → **Set up**.
2. It asks for a **Meta Business portfolio**. Pick yours or create one (just a name).
3. Open **WhatsApp → API Setup**. Meta gives you a free **test phone number** to send from.
4. Copy the **Phone number ID** shown under the "From" number (not the phone number itself):
   ```
   WA_PHONE_NUMBER_ID=123456789012345
   ```

### 2. Allow your own number to receive
1. Still on **API Setup**, under **To**, click **Manage phone number list** → **Add phone number**.
2. Enter your WhatsApp number and type in the code WhatsApp sends you. A test number can message up to 5 numbers.
3. Put your number in `.env`, country code first, no `+`, no spaces:
   ```
   WA_TO=9779800000000
   ```

### 3. Get a token that doesn't expire
The token shown on API Setup expires after 24 hours. It's fine for a first test, but for the hourly report make a permanent one:
1. [business.facebook.com](https://business.facebook.com/) → **Settings** → **Users** → **System users** → **Add**,
   with the **Admin** role.
2. Select the new system user → **Assign assets**. Pick your **app** (full control) and your **WhatsApp account**
   (full control).
3. **Generate new token**: choose your app, expiry **Never**, and tick `whatsapp_business_messaging` and
   `whatsapp_business_management`.
4. Copy it into `.env`:
   ```
   WA_TOKEN=EAAG...
   ```

### 4. Create the message template (so every hourly report arrives)
WhatsApp only lets a business send free text to you within **24 hours of you messaging it**. Outside that window only
**approved templates** arrive, and an hourly report is almost always outside that window. So create one template, once:

1. **WhatsApp Manager** (API Setup page → link, or business.facebook.com → WhatsApp accounts) → **Message templates** → **Create template**.
2. Category **Utility**, name `clipper_report`, language **English (US)**.
3. Body text (the `{{1}}` is where Clipper puts the report):
   ```
   Clipper views update: {{1}}
   ```
   Give the sample value something like `YouTube: 1,200 views | Best platform: YouTube`.
4. Submit. Approval usually takes minutes to a few hours. Then in `.env`:
   ```
   WA_TEMPLATE=clipper_report
   WA_TEMPLATE_LANG=en_US
   ```

Template variables can't contain line breaks, so the report arrives as one line with `|` between parts. Without
`WA_TEMPLATE` it's sent as normal multi-line text, which only arrives if you messaged the test number in the last 24h.

### 5. Test it
Restart the server. On the **Socials** page, the WhatsApp card should have a green dot. Click **Send report now**.
`REPORT_MINUTES=60` in `.env` sets how often it's sent.

**Cost:** Meta may charge a small fee per template message, depending on your country and their current pricing
(check *WhatsApp Business Platform pricing*). Hourly is about 720 messages a month. If that matters, raise
`REPORT_MINUTES` (e.g. 240 = every 4 hours).

---

## Check that it worked

1. Restart the server and open the **Socials** page. Every platform you set up should have a **green dot**.
2. Make a clip with that platform ticked under **Upload to**, or press **Upload** on an existing clip card.
3. If it fails, the platform's own error appears on the clip card and in the red banner. Full details, with
   tracebacks, are on the **Logs** page.

## Common errors

| Error | Fix |
|---|---|
| `facebook not connected (see README)` | `FB_PAGE_ID`/`FB_PAGE_TOKEN` missing in `.env`, or the server wasn't restarted. |
| `(#200) ... permission` / `(#10) ... permission` | A permission is missing: redo Facebook step 2 with every box ticked, then steps 3-4. |
| `Error validating access token` / `Session has expired` | You used the short-lived token. Redo Facebook steps 3-4 (use the Page token). |
| Instagram `media type ... not supported` / `not a business account` | Instagram isn't a Business/Creator account, or isn't linked to the Page. |
| TikTok `scope_not_authorized` | Add `video.publish` / `video.upload` to the app, then redo TikTok step 2. |
| TikTok `unaudited_client_can_only_post_to_private_accounts` | Normal before approval: keep `TIKTOK_PRIVACY=SELF_ONLY`. |
| TikTok `invalid_grant` / token errors | Refresh token expired or used up: redo TikTok step 2. |
| `uploadLimitExceeded` (YouTube) | YouTube's daily per-channel limit. The banner shows a 24h countdown. |
| WhatsApp `(#131030) Recipient phone number not in allowed list` | Add and verify your number under API Setup → To (WhatsApp step 2). |
| WhatsApp `(#132001) Template name does not exist` | Template not approved yet, or `WA_TEMPLATE` / `WA_TEMPLATE_LANG` don't match it exactly. |
| WhatsApp `Invalid OAuth access token` / `Session has expired` | The 24h token expired: make the system-user token (WhatsApp step 3). |
| WhatsApp "sent" but nothing arrives | No `WA_TEMPLATE` and you haven't messaged the number in 24h: set up the template (step 4). |
