# Switch on automatic credit limits

**What this does and why.** Every day the system reads the Experian and Creditsafe alert emails in
your mailbox and the ARR file in SharePoint, recalculates the credit limit for any client whose
rating or recurring revenue changed, puts the PDF summary and a note on the client in Xero, files the
alert email in the client's *Debt & Credit* folder, and emails you one summary. You only reply to the
cases it cannot decide alone. It replaces the *Credit Limit Assessment Workings* spreadsheet.

You do Parts 2–3 once (about 15 minutes). Part 4 is a one-off review
before switch-on. Part 5 is the only thing you do from then on.

---

## Part 1 — Microsoft 365 (nothing to do)

The system uses the Microsoft 365 connection you have already approved in Claude, under your own
login. No administrator is involved and no new permission is needed.

---

## Part 2 — Let Xero accept the PDF and the note (Xero admin, ~10 minutes)

*What this is:* the existing Xero link (set up for account owners) is read-only. Filing a PDF and a
note on a contact needs two more permissions.

1. Go to **developer.xero.com** → **My Apps** → `Sales Orders – account owner sync`.
2. Find the access section (it may be called **Scopes**). Tick `accounting.contacts` (not only the
   ".read" one) and `accounting.attachments`. Click **Save**.
   *If Xero asks the authorising admin to re-approve, they get an email: click the link and approve
   for **Managed247**.*

**If it doesn't look like that:** stop and send a screenshot.

**Done when:** the app shows both new scopes and is still connected to Managed247. *(Done 2 Oct 2026.)*

**Please note:** Xero does not let any system change the **credit limit box** on a contact. Until you
decide otherwise (decision D2), the daily email lists any limit that changed so you can copy it into
that box — or you can stop using the Xero box (recommended: this system holds the limit and its history).

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
2. Under **Needs you**, each line is a client the system would not decide alone (for example, the
   trading need is above half the lower bureau limit). Reply to Claude, e.g.
   "Set Acme at £12,000 because they pay by Direct Debit, review in 6 months."
3. Nothing under **Needs you**? Nothing to do.

**If the email does not arrive by 9am:** tell Claude "the credit run email didn't come" — the run
reports its own problems in the email, so a missing email means it did not run at all.

**Done when:** each morning's **Needs you** list is empty.
