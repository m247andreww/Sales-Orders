# Switch on automatic credit limits

**What this does and why.** Every day the system reads the Experian and Creditsafe alert emails in
your mailbox and the ARR file in SharePoint, recalculates the credit limit for any client whose
rating or recurring revenue changed, puts the PDF summary and a note on the client in Xero, files the
alert email in the client's *Debt & Credit* folder, and emails you one summary. You only reply to the
cases it cannot decide alone. It replaces the *Credit Limit Assessment Workings* spreadsheet.

You do Parts 2b, 2c and 3 once (about 20 minutes). Part 4 is a one-off review
before switch-on. Part 5 is the only thing you do from then on.

---

## Part 1 — Microsoft 365 (nothing to do)

The daily job uses the Microsoft 365 connection you have already approved in Claude, under your own
login. No administrator is involved. Alert emails are **tagged** with the client's name (an Outlook
category such as "Credit: Acme") instead of copied into folders: the connection cannot copy an email,
and it can only see the first 10 client folders.

**To see a client's credit emails in Outlook:** search `category:"Credit: Acme"`.

---

## Part 2 — Xero (done 2 Oct 2026)

The Xero link can now attach the PDF and add a note to the contact. Xero does not let any system change
the **credit limit box** on a contact: the daily email lists any limit that changed (decision D2).

---

## Part 2b — Give the daily job a safe place to keep its records (Azure, ~10 minutes, once)

**What this does and why.** The daily job starts fresh each morning, so it needs somewhere private to
keep the credit database between runs. This creates a locked storage area in Managed247's own Azure
account (UK), keeping every day's version.

1. Go to **portal.azure.com** and sign in as yourself.
2. Click the **Cloud Shell** icon (`>_`) at the top. Choose **Bash** if asked.
   *You should see a black command window at the bottom.*
3. Type `cd Sales-Orders && git pull && bash infra/credit-state.sh` and press Enter.
   *If you see "No such file or directory", type `gh repo clone m247andreww/sales-orders Sales-Orders`
   first, press Enter, then repeat this step.*
4. When it says **DONE**, it shows one long line starting `https://stsocredit…` and a website name
   ending `.blob.core.windows.net`. Leave the window open for Part 2c.

**If it doesn't look like that:** send a screenshot of the window. Do not paste the long line anywhere.

**Done when:** you see **DONE** and the long line.

---

## Part 2c — Tell Claude where it may connect, and store the three codes (~5 minutes, once)

**What this does and why.** Claude's cloud workspace blocks websites by default, and keeps codes in a
protected settings area so they never appear in a chat.

1. In this Claude session, click the **environment name** in the title bar, then **Edit**.
2. Under **Network access**, add these allowed domains: `api.xero.com`, `identity.xero.com`, and the
   website name from Part 2b step 4 (ending `.blob.core.windows.net`).
3. Under **API credentials** (or **Environment variables**), add:
   - `SALES_ORDERS_STATE_URL` = the long line from Part 2b step 4;
   - `SALES_ORDERS_XERO_CLIENT_ID` = the Xero Client ID from your password manager;
   - `SALES_ORDERS_XERO_CLIENT_SECRET` = the Xero Client secret.
4. Click **Save**.

**If it doesn't look like that:** send a screenshot of the settings screen (never of the codes).

**Done when:** saved. Tell Claude "settings done"; Claude then runs a first test day with you watching
and schedules the daily job.

---

## Part 3 — Check the bureaus send their alerts to you (you, ~5 minutes)

1. In **Experian Business Express**, open **Monitoring** and check every client is monitored and that
   alerts go to `andrew.whitford@managed.co.uk`.
2. In **Creditsafe**, open the **Live Customers** portfolio and check every client is in it, with
   email alerts on.
   *Clients missing from either will be listed by the system as "single bureau" or "no bureau limit".*

**Done when:** both portfolios contain your clients.

---

## Part 4 — Go-live review (you, once, ~30 minutes)

*What this is:* before the system sets any limit, you check it has every client right and see where
it differs from the spreadsheet. Claude prepares both files; you only read and answer.

1. Open `data/credit/credit_subjects_draft.json` — the list of monitored companies with each one's ARR
   prefix (the 3 letters at the start of its ARR file reference, e.g. `TST001`). Tell Claude any
   wrong match, and which companies are suppliers or "for information" only.
2. Open `data/credit/go-live-reconciliation.md` — each client: the spreadsheet's limit, the new
   figure, and why they differ.
3. For each limit you want to keep regardless (e.g. a Board decision), tell Claude
   "keep <company> at £<amount> because <reason>, review by <date>".

**Done when:** Claude confirms the list is loaded and your kept limits are recorded.

---

## Part 5 — Every day from then on (you, a minute)

1. Read the email **"Credit run: … limit(s) updated, … decision(s) needed"**.
2. Under **Needs you**, each line is a client the system would not decide alone. Click **Reply** and
   type one line per decision, exactly like this:
   `SET Acme Ltd 12000 BECAUSE pays by Direct Debit REVIEW 2027-04-01` (the REVIEW part is optional).
   Send it. The next morning's email confirms it, or lists it under "Not understood".
3. Nothing under **Needs you**? Nothing to do.

**If the email does not arrive by 9am:** tell Claude "the credit run email didn't come" — the run
reports its own problems in the email, so a missing email means it did not run at all.

**Done when:** each morning's **Needs you** list is empty.
