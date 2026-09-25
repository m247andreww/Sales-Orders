# Build the Sales Orders database in Azure

**What this does and why.** Builds Managed247's own copy of the Sales Orders database in Microsoft's
UK South data centre, with daily backups copied to UK West, passwords locked in a vault and a lock that
stops anyone deleting it. You paste a few lines into Azure's built-in command window (Cloud Shell);
a script does the rest and asks before it changes anything. About 30 minutes, mostly waiting.

**You need:** to be signed in to portal.azure.com as andrew.whitford@managed.co.uk, with the
**Owner** role on the Azure subscription that will pay for it (roughly £20–£40 a month, to be
confirmed in Azure Cost Management after the first month).

## Part 1 — Open Cloud Shell

1. In the top bar of portal.azure.com, click the **>_** icon (just right of **Copilot**).
   *A panel opens at the bottom of the screen.*
2. If asked **Bash or PowerShell**, click **Bash**.
3. If asked about storage, click **No storage account required**, pick your subscription, click **Apply**.
   *You should see a line ending in `$` — that is where you type.*

## Part 2 — Get the project from GitHub (read-only)

4. Type `gh auth login` and press **Enter**. Answer each question with the arrow keys and **Enter**:
   **GitHub.com** → **HTTPS** → **Yes** → **Login with a web browser**.
5. It shows a code like `ABCD-1234`. Open **github.com/login/device** in a new browser tab, sign in
   to GitHub, type the code, click **Authorize**. Go back to the Azure tab and press **Enter**.
   *You should see "Logged in as …".*
6. Type this line exactly and press **Enter**:
   `gh repo clone m247andreww/Sales-Orders -- --branch claude/peaceful-tesla-enhyce`
7. Type `cd Sales-Orders` and press **Enter**.

## Part 3 — Run the build

8. Type `az account set --subscription "Azure subscription 1"` and press **Enter** (this picks the
   active subscription; the other one, MCPP, is disabled). *Nothing is shown if it worked.*
9. Type `bash infra/deploy.sh` and press **Enter**.
10. It shows who you are signed in as and which subscription will be billed. If both are right, type
   `yes` and press **Enter**. If not, type anything else to stop.
11. Leave the window open. Each stage prints `==> 1/8`, `==> 2/8` … Stage 3 takes 10–15 minutes.
    *At the end you should see* **DONE.** *and three lines (Server, Key Vault, Protection).*
12. Copy those three lines back to Claude.

**If it doesn't look like that:** the script stops at the first problem with **STOPPED:** and a reason,
and nothing after that point is changed. Send a screenshot of the Cloud Shell panel. Running
`bash infra/deploy.sh` again afterwards is safe.

**Done when:** you see **DONE.** Next, Claude loads the real master data, the Register and Greg's
leaver steps, then runs the first Xero check for your review.
