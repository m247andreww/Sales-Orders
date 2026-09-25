# Keep the staff list up to date (Sales Orders - Reference sheet)

**What this does and why.** The database takes its staff list from the Google Sheet
**Sales Orders - Reference** each night. That list covers who sells, which names they have on the
Register, which Xero group marks their accounts, and when they left. If the sheet is wrong, net new /
existing coding and account ownership will be wrong too. Changes take effect the next morning.

Open it from Google Drive (signed in as m247.andreww@gmail.com): **Sales Orders - Reference**.

## When someone joins

1. Open the **Staff** tab and click the first empty row.
2. Fill in **Name**, **Work email** (their @managed.co.uk address) and **Job title**.
3. **Register labels**: the name exactly as it will appear in the AW SOs Salesperson column.
4. **Xero owner group**: the Xero contact group for their accounts, exactly as it is named in Xero
   (e.g. `e. Sam`). Create the group in Xero first.
   *Leave Last working day empty.*

## When someone leaves

5. On their row, fill in **Last working day** as dd/mm/yyyy. If they are on gardening leave, enter
   the day before the gardening leave started.
   *From the next morning their accounts show as House.*
6. When their accounts go to a successor, open the **Account moves** tab and add a row:
   - **From**: the leaver's email;
   - **To**: the successor's email, or `House`;
   - **Effective date**: dd/mm/yyyy;
   - **Reason**: who decided, and when.

## Rules

- Do not rename the tabs or change the headings. If they change, the nightly load stops and tells
  you why.
- Do not delete rows to undo something. Add a new Account moves row instead.
- The database never writes to this sheet.

**If something looks wrong the next morning:** ask Claude to run `sales-orders job-status`, or look in
your inbox for the "nightly sync" alert. The job's log says which row it rejected and why.

**Done when:** the next morning's run shows `reference-staff` as succeeded.
