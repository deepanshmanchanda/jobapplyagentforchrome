# Job Application Pipeline

A local Streamlit workspace for collecting job listings from permitted sources, scoring them, classifying risk, reviewing jobs in batches, and tracking outcomes. The pipeline database is stored at `data/jobs.sqlite3` by default.

## Run it

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
streamlit run app.py
```

Open **Job pipeline** to ingest and rank listings, **Application assistant** to run the local Chrome-connected application agent or analyze a form manually, and **Application tracker** to review application history, reusable answers, and the browser-agent audit trail.

To report response rates from the command line:

```powershell
python accuracy_report.py
```

Run offline tests with:

```powershell
python -m unittest discover -s tests -v
```

## Job intake

There is no scraper or unapproved job-site client. Import a CSV exported from a permitted API/feed, or add a listing manually. The CSV requires `url`; optional columns are:

```text
url,title,company,location,description,application_flow_type,screening_questions_answerable,location_match,seniority_match,visa_match,sponsorship_status,sponsorship_evidence,sponsorship_provenance
```

Use `easy_apply`, `custom_ats`, or `unknown` for `application_flow_type`. Boolean match columns must explicitly contain `true`, `yes`, `1`, `match`, or `answerable` to count as true. Do not mark an answerable field unless the answer is in your own configured facts. URLs are canonicalized to remove common tracking parameters before the SQLite uniqueness check.

`sponsorship_status` must be `evidence_found`, `unknown`, or `no_sponsorship`. Record the listing quote or other evidence in `sponsorship_evidence` and where it was verified in `sponsorship_provenance`. Missing evidence stays **unknown**; it is not treated as a negative result, but it cannot qualify for the dry-run auto tier when sponsorship is required. Listings with verified evidence are displayed ahead of unknown listings, separately from skill score.

An Apify or other provider connector is not included because this workspace has no actor ID, permitted API contract, or credentials configured. Any connector should use an approved API and feed normalized jobs into `JobStore.add_job` or `import_csv`.

## Scores and risk tiers

`config.yaml` is the source of truth for minimum and auto-tier score thresholds, scoring weights, application flow requirements, required constraint matches, sponsorship handling, worker limits, and daily volume cap. The starter thresholds are 75 for keeping a job and 85 for auto-tier eligibility. Edit the file to tune them; refresh the Streamlit page after saving.

The current scorer is an offline, deterministic skill/title keyword-overlap baseline. It returns structured score, reason, matched keywords, missing keywords, and a comparison of configured resume skills against catalogued skills named in each role description. It is not an LLM and does not understand context such as negation, synonyms, or equivalent job titles. The project has no model provider configured, so do not treat these scores as semantic job-fit scores.

The **Configuration** tab stores local search preferences in the SQLite database: redundancy target date, acceptable work locations, salary band, sponsorship requirement, visa note, and target job titles. Target titles and sponsorship requirement are applied to new dashboard scoring; locations and salary remain displayed preferences for human review until a permitted feed provides comparable structured data.

Risk classification:

- Below the configured minimum is rejected with a reason.
- A custom ATS listing with unanswerable required screening questions is rejected when that rule is enabled.
- An explicit no-sponsorship statement is rejected when sponsorship is required in config.
- The `auto_submit` tier means only that a listing passes the configured dry-run checks: easy-apply flow, score threshold, answerable screening questions, and exact location/seniority/visa match flags.
- Other passing jobs enter the batch review queue.

Location, seniority, and visa exact-match flags are supplied by the reviewer or trusted feed. The app does not infer these facts or invent screening answers. Empty visa, right-to-work, notice, and salary values in the config are intentionally unanswered.

## Chrome side-panel application agent (recommended)

Workday repeated sections now pass their row metadata through to the local planner. Autopilot can open Add/Add another for detected employment, education, and certification sections, fill supported fields from explicitly parsed resume records, and select matching qualification options. It observes each new row before adding another and preserves existing answers. Resume parsing remains format dependent; missing facts and unrecognized controls still need attention. SAP and Oracle custom dialogs do not yet have equivalent repeated-section support. Stale ordinary actions are replanned up to twice when a dynamic page replaces its controls; submission and account actions are excluded from automatic retries.

Normal job-application work happens in the **Application Autopilot** Chrome side panel, beside the employer application page. Streamlit remains useful for maintaining the local profile, reviewed answer library, and audit history, but it is not the control surface for each application. The separate loopback-only companion service at `127.0.0.1:8765` keeps the extension available without Streamlit.

Install the local extension once:

1. Open `chrome://extensions`.
2. Turn on **Developer mode**.
3. Click **Load unpacked** and select this project's `chrome_extension` folder.
4. Pin **Job Application Copilot Bridge** from Chrome's extensions menu.
5. On an employer application page, click the pinned extension icon to open **Application Autopilot** beside the page.
6. Leave **Submit completed applications automatically** checked and click **Start Autopilot**. It binds the current tab, fills saved details, uploads your saved resume, drafts supported answers from your experience, and continues through the application.
7. When a normal field needs an answer, use **Tell the agent what to use** in the side panel: type `Continue` after a manual step, or `Answer exact visible field label: answer` to fill that one current field. Keep **Remember** checked to reuse that answer only for the same exact question on the same employer site.
8. Ordinary manual typing is also learned locally from trusted browser input, so a later matching field on that employer site can be filled without retyping. Passwords, verification, and legal/sensitive declarations are excluded from this learning.
9. With automatic submission enabled, Autopilot submits the completed application and waits for employer confirmation before recording it as submitted. Missing facts, unsupported controls, and verification steps pause the run for your attention. Uncheck automatic submission before Start if you prefer to submit yourself.

**Employer account registration:** Leave **Create an employer account when needed** checked before Start to follow recognised Create account/Register/Sign up links and complete supported registration forms. The agent fills your saved profile email and details, generates a unique 24-character password per HTTPS site and email, fills password confirmation, and creates the account when the form is ready. It then continues through the application in the same tab. Required terms, email verification, CAPTCHAs, unknown facts, and unsupported password rules pause the flow for your attention. It does not access your inbox or invent answers. Existing browser-filled logins continue to work.

Generated passwords are stored in `data/jobs.sqlite3` encrypted with Windows DPAPI for the Windows user running the companion. They are not included in page snapshots or Chrome command receipts. Open **Application tracker â†’ Employer accounts â†’ Access saved password** to retrieve one for a later sign-in. A stored password means it was prepared, not that the employer confirmed account creation. Passwords changed manually on the employer site are not synced back to this store. Keep access to the original Windows account; copying the database alone to another computer/user does not make its passwords readable. Automatic password generation requires Windows.

You can repeat step 6 on several application tabs. Each explicitly started tab has its own isolated run and can be prepared concurrently; the agent never switches or cross-fills tabs.

There is no normal Pair, Refresh, or Snapshot sequence. The Start button performs the initial tab binding and snapshot automatically; the panel then displays the current step, safe fields visible, progress, validation messages, and any pause reason. Candidate values stay in the local service/database and are not displayed unnecessarily in the panel.

When the extension source is updated, click **Reload** for it on `chrome://extensions` once (current version: **0.9.0**), then reload the application page and reopen its side panel. To replace an already running companion after a backend update, run `./start_agent_service.ps1 -Restart`.

The live companion now enables model-backed action decisions for every Autopilot command batch: ordinary fills, dropdowns, uploads, repeated-section Add buttons, navigation, account actions, and authorized submission. Local adapters still discover supported controls and prepare grounded candidates; the model chooses among those candidates using visible page context, errors, and recent results. It cannot create arbitrary selectors, change supplied facts, or bypass submission settings. Narrative answers use the model with saved fact references instead of the template drafter. `OPENAI_AGENT_MODEL` selects the decision/drafting model, falling back to `OPENAI_INSTRUCTION_MODEL` and then the app's `gpt-5-mini` default.

Each run permits at most 20 attempted model requests across action decisions, narrative drafting, and instruction interpretation. Status text reports attempted requests; cached decisions do not count. Continue cannot bypass an exhausted limit. Starting a new run resets this request limit, so it is not a daily spending cap or a dollar estimate. When the employer reports validation errors after a submission attempt, instructions can correct the form without treating the run as absent. The earlier submission claim remains locked until its outcome is verified, preventing an automatic duplicate submission. Required visible manual-only questions also block submission before a click.

Page text, ordinary field values/options and proposed actions are sent to the model; narrative drafting also sends saved resume text and eligible profile facts. Generated passwords and uploaded file bytes stay in the local executor. Requests use `store=False` and no SDK retries. Action decisions have a 60-second timeout; the companion retries transient decision failures once after a short delay, then reports a specific error category. Drafting and instruction interpretation retain their 30-second timeouts. Model failures never revert to rule-only action execution. Valid plans containing multiple structural actions execute only the first selected action before observing and replanning. Unchanged decisions are cached, obsolete page decisions are discarded, and unchanged actions stop after two attempts. API usage and latency therefore increase with the number of application steps. Unknown widgets still require an adapter; model planning does not establish universal ATS support.

The instruction box accepts contextual answers such as `it's a technical diploma`. Recognized degree descriptions can identify the Degree field even when its menu is closed; visible option matches and failed-field context also help resolve the target. The panel shows the structured interpretation and asks for clarification when ambiguous. With Remember checked, degree corrections persist for a uniquely matched resume education record on that employer's site. Unidentified resume records remain application-only corrections. Workday dropdowns are selected one at a time. Arbitrary browser procedures are not learned from text instructions.

Workflow requests and unmatched natural-language replies now use a structured instruction interpreter through the existing OpenAI provider. It receives the instruction, ordinary field labels/options, and parsed education records; it does not receive passwords or the full resume file. `OPENAI_INSTRUCTION_MODEL` overrides the existing app default of `gpt-5-mini`; requests have a 30-second timeout, no automatic API retries, and `store=False`. Supported decisions are a literal field answer, selection of saved education records, or clarification. Selecting only an engineering degree filters the education plan and replaces known values in the current rows; it does not delete extra rows automatically. Remembered education selections are scoped to the employer and the parsed resume records. API failures or invalid decisions never fall back to entering the instruction as field text. This is not a general browser-command executor.

To run the extension without Streamlit, start the local companion once after signing in to Windows:

```powershell
.\start_agent_service.ps1
```

If PowerShell scripts are disabled, run `.\.venv\Scripts\python.exe agent_service.py` from the project folder and leave that terminal open.

It runs only on `127.0.0.1` and keeps the profile/planner available while the extension prepares application tabs.

### Legacy Chrome debugging fallback

The **Autonomous agent** tab is a local, user-controlled application runner for links you supply. It connects to a separate Chrome profile through a loopback-only debugging connection. If Streamlit is running as a background service, run the following in your own VS Code PowerShell terminal to open the browser on your desktop, then click **Connect to Chrome** in the dashboard:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_agent_chrome.ps1
```

The desktop launcher uses `http://127.0.0.1:9222`; it does not expose the browser to the network. Sign in to an employer site directly in that Chrome window; the agent does not collect passwords, read or enter one-time codes, solve CAPTCHAs, or bypass access controls. If an employer requires account creation, complete the password and any email/phone verification yourself in that Chrome profile; Chrome's own password manager can retain credentials without exposing them to the app.

You do not need to send job links to a chat once the dashboard is running. In **Autonomous agent**, connect to Chrome and use **Paste a job or ATS link â†’ Open pasted link in agent Chrome**. The app opens the URL in the agent browser, reads available job context, and places it in the run form. **Refresh Chrome tabs** updates the selectable list. Only the separately launched, agent-enabled Chrome profile is visible to the app; ordinary Chrome windows are not attached automatically because that would expose unrelated browsing data.

If an extension or a still-loading page temporarily prevents Playwright from attaching to every browser control, the dashboard enters a clearly labelled tab-detection mode. It continues to list, refresh, select, and open agent-Chrome tabs; reconnect when the browser becomes available before running form automation.

## Legacy dashboard controls

The dashboard controls below remain available only for recovery and diagnostics. For normal use, open the extension side panel on the application page and choose **Start Autopilot**.

The local extension in `chrome_extension` pairs the application tab you explicitly select with the dashboard. This is the primary autonomous route: it uses only a loopback server at `127.0.0.1:8765`, never uploads page data to a remote service, and keeps every supported action in that one paired tab.

Install it once in Chrome:

1. Open `chrome://extensions`.
2. Turn on **Developer mode**.
3. Click **Load unpacked** and select this project's `chrome_extension` folder.
4. Pin **Job Application Copilot Bridge** from Chrome's extensions menu.
5. Only when diagnosing a stalled older tab, open the employer application tab and use the legacy **Pair active tab** control.
6. The dashboard's paired-tab controls can then inspect or recover that diagnostic run.

When the extension source is updated, click **Reload** for it on `chrome://extensions` once (current version: **0.5.9**). **Sync form snapshot** is only a legacy recovery control; it is not needed after every page.

After one explicit start, Autopilot fills source-backed profile fields, uploads the saved resume, drafts supported narrative answers, and follows recognised controls such as **Apply Manually**, **Save and Next**, **Next Step**, and **Continue**. It is bound to the paired tab ID. With automatic submission enabled, it submits once required fields are complete and waits for confirmation. It pauses for unresolved facts, unsupported uploads or controls, sensitive declarations, and access steps requiring your attention. Commands are document-bound and acknowledged before the next page step; persisted submission claims prevent duplicate attempts.

The extension excludes password and verification values from snapshots. Chrome's password manager remains responsible for credentials; the agent can continue a sign-in form already filled by Chrome. Resume uploads and final submission require an active run with the corresponding authorization. OTP and CAPTCHA steps require your attention.

Choose one of three policies before running a job:

- **Review**: inspect and save the form plan only.
- **Hybrid**: continue only when the recorded fit score meets your threshold and sponsorship evidence is verified when required.
- **Auto**: continue every eligible job.

Final submission is always performed by you in the employer page after review. Unknown required fields, unfamiliar controls, authentication prompts, CAPTCHAs, and ambiguous sponsorship evidence stop the run and are recorded in the local audit trail.

The agent now recognises Oracle Recruiting pages, including its document dialog and employment section. With **Use supported ATS workflow steps** selected, it attaches the locally stored CV once and adds only records in **Your profile â†’ Verified employment history**. Employment dates must be exact (`YYYY-MM-DD`) facts entered by you; month-only CV dates are deliberately rejected. Networx and standard HTML forms continue to use DOM-based field inspection.

**Use OpenAI to draft safe narrative answers** is an optional per-run setting. It uses your local API key and selected model to draft only unresolved text/textarea answers from the job description and your reviewed answer library. The draft validator rejects answers for passwords, codes/CAPTCHAs, work-rights or immigration, salary/notice/start dates, address, NI number, equality monitoring, criminal declarations, and other protected questions. Review mode and final submission remain independent controls.

## Review, safety, and audit

The application assistant inspects the current visible inputs, textareas, and dropdown choices, builds a plan with required status and proposed source, and applies only source-backed values. Resume-grounded drafts cover the three RHP questions shown during the current session. The personal-service story stays blank because it is not in the resume. Unknown selections, street address, county, eligibility claims, and unsupported answers are listed for you rather than guessed.

The local application ledger stores role, company, URL, stage, submitted timestamp, resume/role skill lists, skill gaps, and the form fields/values applied. The application is recorded as in progress after a reviewed form plan is applied; mark it submitted yourself after submitting in the employer's page. The reusable response library stores edited answers by question category and offers them as future drafts; review each one for the current employer and wording before applying. Candidate contact details and the uploaded resume are stored in the same local SQLite database for reuse across sessions.

The pipeline's `auto_apply_enabled` flag still applies only to its dry-run job tier. Actual application automation is initiated from the Application assistant's local Chrome agent and uses its explicit policy controls. `max_concurrency` currently caps local scoring work only; there is no unattended cloud queue or platform bypass. Platform error state can be recorded and paused; no code attempts to work around CAPTCHA, login prompts, or site blocks.

The browser is non-headless and accepts direct HTTPS links from systems such as Greenhouse, Lever, Ashby, and Workday, as well as Indeed listings. Dynamic pages are analyzed from their current DOM each time; multi-step forms must be analyzed again after navigation. This is not an LLM-based universal ATS agent: unusual widgets and wording outside supported workflows will be shown as unresolved. Gmail confirmation tracking and OAuth are not implemented yet. The app never reads or enters one-time codes.

Candidate facts, resume bytes, job history, answer library, and application snapshots are stored in the local SQLite database. Back up or delete `data/jobs.sqlite3` with the rest of your application data.

## Accuracy report

The local `outcome` values are `rejected`, `interview`, `offer`, `hired`, `ghosted`, and `unknown`. The CLI compares recorded response rates for auto-tier eligible listings and batch-reviewed approved listings. Employer rejection counts as a response; ghosted does not. Unknown outcomes are excluded from the denominator.

In this dry-run build, the auto-tier group contains eligible listings, not actual auto-submissions. Use the comparison to evaluate ranking/review signals only; it is not evidence of submission accuracy. Do not tune the threshold from a small sample. Add enough recorded outcomes and inspect false positives before changing the configuration.

## Not implemented yet

- Approved daily job-feed/API connector and scheduled ingestion.
- Semantic job scoring and universal open-ended form analysis. The optional OpenAI provider is limited to fact-constrained narrative drafts; it does not decide eligibility or create candidate facts.
- Gmail OAuth integration to associate application acknowledgement and interview messages with ledger entries.
- Unattended cloud queue workers, daily notifications, and direct LinkedIn/Indeed-specific automation modules. The local Chrome agent supports user-supplied employer/ATS links after the user signs in.
- Automatic tailored resume/cover-letter generation. The store provides a per-job cache API for a future generator that uses verified candidate facts only.

These boundaries are deliberate: the app must not scrape prohibited sources, bypass blocks, invent answers, or submit applications without a permitted integration and an explicit review policy.
