# Connect Xero so the database can read account owners

**What this does and why.** Xero records who owns each customer account in its contact groups.
This sets up a read-only link so the database can check those groups every day by itself, with
nobody downloading anything. It is a paid Xero add-on ("Custom connection").

**Who can do it:** someone who is an admin of Managed247's Xero. About 15 minutes.

## Part 1 — Create the app

1. Go to **developer.xero.com** and sign in with your normal Xero login.
2. Click **My Apps** at the top, then **New app**.
   *You should see a box headed "Add a new Xero app".*
3. **App name:** type `Sales Orders – account owner sync`.
4. **Integration type:** click **Custom connection**.
5. **"Will you use Xero data to train… an AI model?"** click **No**.
6. **"…Xero's minimum security requirements?"** click **Yes**.
7. **Company or application URL:** type the company website in full, starting `https://`
   (for example `https://www.managed.co.uk` — use the real site address).
   *If you see "Enter a valid company or application URL", the `https://` is missing.*
8. Tick **I have read and agree…**, then click **Create app**.

## Part 2 — Choose what it may see

9. On the app's page, find the section for choosing access (it may be called **Scopes**).
10. Tick **only** the read-only contacts option (it may be shown as `accounting.contacts.read`).
    Tick nothing else.
    *If the list looks different, stop and send a screenshot before ticking anything.*
11. Choose the person who will authorise the connection (a Xero admin), then click **Save**.
12. Check the price shown before you confirm. Xero emails the authorising person — they click the
    link in that email and approve it for **Managed247**.

## Part 3 — Keep the two codes safe

13. Xero now shows a **Client ID** and, after clicking **Generate a secret**, a **Client secret**.
    These work like a username and password for the link.
14. Save both in your password manager as "Xero – Sales Orders sync".
    **Never** paste them into email, Teams or a chat (including Claude).

**If anything doesn't look like the above:** stop and send a screenshot of the screen you are on.

**Done when:** the app shows as connected to Managed247 and both codes are in your password
manager. **Next:** once hosting is chosen, the codes are moved into the server's secure store and
the first check is run; you review the list of differences before anything is changed.
