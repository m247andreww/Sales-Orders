# Switch on automatic credit limits

**What this does and why.** Every day the system reads the Experian and Creditsafe alert emails in
your mailbox and the ARR file in SharePoint, recalculates the credit limit for any client whose
rating or recurring revenue changed, puts the PDF summary and a note on the client in Xero, and fills
in one page, the **Credit Desk**. You only decide the cases it cannot decide alone, on that page. It replaces the *Credit Limit Assessment Workings* spreadsheet.

You do Parts 2b, 2c and 3 once (about 20 minutes). Part 4 is a one-off review
before switch-on. Part 5 is the only thing you do from then on.

---

## Part 1 — Microsoft 365 (nothing to do)

The daily job uses the Microsoft 365 connection you have already approved in Claude, under your own
login. No administrator is involved. That connection can only **read** (tested 2 Oct 2026): it cannot
send, tag, move or copy emails. Instead:

- **Your one Outlook rule** (set 2 Oct 2026) moves each alert to Inbox / "Credit alerts" and marks it read.
- **Each client's alert is filed in Xero**: a one-page PDF of that client's part of the alert goes on its
  Xero contact, next to the credit limit PDF. This replaces the Outlook "Debt & Credit" folders for credit.
- A record of every alert is also kept in the credit database.

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
3. Copy the whole grey box below (from `bash <<'EOF'` to the last `EOF`), paste it into the black
   window and press Enter. *After about a minute you should see **DONE** and two numbered lines.*

```
bash <<'EOF'
set -euo pipefail
RG=rg-salesorders-prod
SUB=""
for S in $(az account list --query "[?state=='Enabled'].id" -o tsv); do
  if [ "$(az group exists -n $RG --subscription $S)" = "true" ]; then SUB=$S; break; fi
done
if [ -z "$SUB" ]; then echo "STOPPED: no active subscription holds $RG. Send a screenshot."; exit 1; fi
az account set --subscription $SUB
echo "Using subscription: $(az account show --query name -o tsv)"
echo "Switching on Azure Storage (1-2 minutes)..."
az provider register --namespace Microsoft.Storage --wait -o none
ACC=$(az storage account list -g $RG --query "[?tags.purpose=='credit-state'].name | [0]" -o tsv)
if [ -z "$ACC" ]; then ACC=stsocredit$(openssl rand -hex 4); az storage account create -g $RG -n $ACC -l uksouth --sku Standard_GRS --kind StorageV2 --https-only true --min-tls-version TLS1_2 --allow-blob-public-access false --tags purpose=credit-state -o none; fi
az storage account blob-service-properties update -g $RG -n $ACC --enable-versioning true --enable-delete-retention true --delete-retention-days 30 -o none
az storage account keys renew -g $RG -n $ACC --key key1 -o none 2>/dev/null
echo "New key made; waiting for Azure to accept it..."
KEY=$(az storage account keys list -g $RG -n $ACC --query "[0].value" -o tsv)
for i in 1 2 3 4 5 6 7 8 9 10; do az storage container create --account-name $ACC --account-key "$KEY" -n credit-state -o none 2>/dev/null && break; sleep 15; done
az storage container show --account-name $ACC --account-key "$KEY" -n credit-state -o none
SAS=$(az storage container generate-sas --account-name $ACC --account-key "$KEY" -n credit-state --permissions rcw --expiry $(date -u -d '+12 months' +%Y-%m-%dT%H:%MZ) --https-only -o tsv)
echo; echo "DONE"
echo "1) Website for Claude network settings:  $ACC.blob.core.windows.net"
echo "2) Code for SALES_ORDERS_STATE_URL:  https://$ACC.blob.core.windows.net/credit-state?$SAS"
EOF
```

4. Leave the window open: line 1 and line 2 are needed in Part 2c. Each run makes a new line 2 and
   switches the old one off, so run it again if line 2 is ever shown to anyone.

**If it doesn't look like that:** send a screenshot of the window (it is safe to paste the box again). Never paste line 2 into a chat or email.

**Done when:** you see **DONE** and the two lines.

---

## Part 2c — Tell Claude where it may connect, and store the three codes (~5 minutes, once)

**What this does and why.** Claude's cloud workspace blocks websites by default, and keeps codes in a
protected settings area so they never appear in a chat.

1. In this Claude session, click the **environment name** in the title bar, then **Edit**.
2. Under **Network access**, add these allowed domains: `api.xero.com`, `identity.xero.com`, and the
   website name from Part 2b step 4 (ending `.blob.core.windows.net`).
3. In the **Environment variables** box (there is no separate credentials section), add one line each, written `NAME=value` with no spaces:
   - `SALES_ORDERS_STATE_URL` = the code on line 2 from Part 2b;
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

**What this does and why.** Each weekday morning the job fills in the Credit Desk page and Claude
notifies you when it has finished. Anything it would not decide alone waits there for you. Your
decision is applied straight away (limit, Xero PDF and note), not the next morning.

1. Open the **Credit Desk**: https://claude.ai/artifact/RuebneZC7AjWuvfiVj2nF8 (pinning it in your
   Claude sidebar saves finding the link).
   *You should see "Last credit run:" with today's date, and a list headed "Needs your decision".*
2. For each client under **Needs your decision**, in the box **Your decision**:
   1. **Limit:** tick "Accept the recommended £…", or tick "A different amount" and type it (whole pounds).
   2. **Reason:** type why (always required), e.g. "pays by Direct Debit".
   3. **Review / follow up on:** press "1 week", "2 weeks", "Last working day of this month" (skips weekends and bank holidays), "1 month" or "3 months", or
      "Pick a date" (always required; after today). The box shows the date chosen, with its weekday.
   4. Click **Confirm decision**. If a step is missing, the box says which.
   *Within about 5 minutes the line should say "Done at HH:MM: limit £…".*
3. Under **Customers with no credit limit** (customers in the ARR file nobody monitors; "new" = first seen
   today): press **Copy** next to the company number, add it in Experian (Monitoring) and Creditsafe (Live
   Customers), then press **I've added it**. *Within about 5 minutes the line disappears: it is now a client.*
   No number shown? Type the 8-character Companies House number into the box first.
4. Under **First figures needed** (clients the bureaus have not yet sent a figure for, largest amount owed
   first), for each client:
   1. Press **Copy** next to the company number.
   2. In Experian, search for it and note the **Credit Limit** (not the Credit Rating) and the risk band.
      In Creditsafe, search for it and note the **Credit Limit**.
   3. Back on the Credit Desk, type each limit in whole pounds (e.g. "120000"). If a portal shows no limit,
      tick **No limit shown**. If the company is not in that portal, leave that line blank.
   4. Choose the Experian risk band if you saw one, then click **Save figures**.
   *Within about 5 minutes the line says "Saved": the client is reassessed, and either gets its limit
   automatically or appears under **Needs your decision**.*
5. Nothing under any list? Nothing to do.

**If it doesn't look like that:** if the date is not today's by 9am, or a line has not said "Done" after 10 minutes, tell Claude "the Credit Desk didn't update" and send a screenshot of the page.

**Done when:** the **Needs your decision** list is empty and every decision reads "Done".
