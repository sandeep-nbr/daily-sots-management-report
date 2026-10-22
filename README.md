# NBR SOTS – Daily Management Report

Builds and emails the daily SOTS management report (tables in the email body + PDF attachment)
from Salesforce data. A scheduled Claude routine runs this every evening.

## Daily run (what the routine does)

1. `D=$(TZ=Asia/Kolkata date +%F)` – the report day (IST).
2. `python3 generate_report.py $D` prints the 7 SOQL queries for that day.
3. Run each with the NBR_SALESFORCE `soqlQuery` tool and save the `records` array to
   `data/$D/<name>.json` (only the fields in the query are needed; drop `attributes`).
   If any result says `"done": false`, stop and report – the day is too large for one page.
4. `python3 generate_report.py $D --build` → `output/$D/` gets `report.html`, the PDF and
   `sendmail_payload.json` (Microsoft Graph sendMail body). `pip install reportlab` if missing.
5. Send by calling the Make tool **Send NBR SOTS report (PDF attached)** (scenario 9925547)
   with `payload` = the contents of `sendmail_payload.json`. It sends from sandeep@nbrgroup.in.
6. Confirm the mail is in Sent Items with the PDF attached, then commit `data/$D` and `output/$D`.

## Metric definitions

| Table | Source |
|---|---|
| Leads by source / user | `Lead__c` created that IST day, by `Lead_Source__c` / `Lead_Sub_Source__c`, owner |
| Calls by user | `Call_Detail__c` started that day; outbound attributed by `Call_From__c`, inbound by `Call_To__c`, matched to the user's phone. Connected = `Call Complete`/`ANSWER`, Busy = `Customer Busy`. Talk time = sum of `Duration_in_Minutes__c` |
| Site visits (done, by source) | `Site_Visit__c` with status Done that day, plus walk-in leads with `Site_Visit_Done_Date__c` = day |
| Follow-ups | Tasks `Follow Up…` due that day. Done = task completed, or a call logged to that lead that day. Missed = scheduled − done |
| Site visits scheduled/done/re-scheduled | `Site_Visit__c` starting that day (excluding cancelled and visits with no lead) plus walk-ins |

Team roster, phone numbers and recipients live in `config.json`. Users not in the roster are
shown as "Other owners".
