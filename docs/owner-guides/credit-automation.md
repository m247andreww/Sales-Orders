# Switch on automatic credit limits

**What this does and why.** Every day the system reads the Experian and Creditsafe alert emails in
your mailbox and the ARR file in SharePoint, recalculates the credit limit for any client whose
rating or recurring revenue changed, puts the PDF summary and a note on the client in Xero, files the
alert email in the client's *Debt & Credit* folder, and emails you one summary. You only reply to the
cases it cannot decide alone. It replaces the *Credit Limit Assessment Workings* spreadsheet.

You do Parts 1–3 once (about an hour in total, mostly waiting for admins). Part 4 is a one-off review
before switch-on. Part 5 is the only thing you do from then on.

---

## Part 1 — Let the system read your mailbox and the ARR file (Microsoft 365 admin, ~20 minutes)

*What this is:* a "service login" for the system in Microsoft 365, limited to your mailbox and the
Finance SharePoint site. It works without anyone signing in.

**Who:** your Microsoft 365 administrator (IT). Send them this part.

1. Go to **entra.microsoft.com** → **App registrations** → **New registration**.
2. **Name:** type `Sales Orders – credit automation`. Leave everything else. Click **Register**.
   *You should see the app's Overview page with an "Application (client) ID".*
3. Click **API permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions**.
4. Tick `Mail.ReadWrite`, `Mail.Send` and `Sites.Selected`. Click **Add permissions**.
5. Click **Grant admin consent for Managed247** and confirm.
   *You should see three green ticks under "Status".*
6. Click **Certificates & secrets** → **New client secret** → description `credit automation`,
   expiry **24 months** → **Add**. Copy the **Value** straight into the password manager as
   "M365 – credit automation" with the **Application (client) ID** and the **Directory (tenant) ID**
   from the Overview page. **Never** paste it into email, Teams or a chat (including Claude).
7. Restrict it to your mailbox only (Exchange Online PowerShell, as an Exchange admin):
   `New-ApplicationAccessPolicy -AppId <Application (client) ID> -PolicyScopeGroupId andrew.whitford@managed.co.uk -AccessRight RestrictAccess -Description "Credit automation: CFO mailbox only"`
   *You should see the policy listed with AccessRight "RestrictAccess".*
8. Give it read access to the Finance SharePoint site only (Graph Explorer or PowerShell, as a
   SharePoint admin): grant the app **read** on the site `FinanceInternal`.
   *Ask IT to confirm "read granted on FinanceInternal".*

**If it doesn't look like that:** stop and send a screenshot of the screen you are on.

**Done when:** IT confirms steps 5, 7 and 8, and the three codes are in your password manager.

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

**Done when:** the app shows both new scopes and is still connected to Managed247.

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
