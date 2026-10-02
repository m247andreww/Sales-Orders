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
az storage account keys renew -g $RG -n $ACC --key primary -o none
KEY=$(az storage account keys list -g $RG -n $ACC --query "[0].value" -o tsv)
az storage container create --account-name $ACC --account-key "$KEY" -n credit-state -o none
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

1. Read the email **"Credit run: … limit(s) updated, … decision(s) needed"**.
2. Under **Needs you**, each line is a client the system would not decide alone. Click **Reply** and
   type one line per decision, exactly like this:
   `SET Acme Ltd 12000 BECAUSE pays by Direct Debit REVIEW 2027-04-01` (the REVIEW part is optional).
   Send it. The next morning's email confirms it, or lists it under "Not understood".
3. Nothing under **Needs you**? Nothing to do.

**If the email does not arrive by 9am:** tell Claude "the credit run email didn't come" — the run
reports its own problems in the email, so a missing email means it did not run at all.

**Done when:** each morning's **Needs you** list is empty.
