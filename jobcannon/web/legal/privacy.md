# Job Cannon — Privacy Policy


**Effective date:** 2026-08-28

**Last updated:** 2026-08-28

## 1. Who we are

Job Cannon ("the Service") is operated by Senkichi, LLC ("we", "us"). We are the
controller of the personal data described in this policy. You can reach us at
hello@jobcannon.dev.

The Service is a job-search tool. It maintains a shared corpus of job postings gathered
from employers' own careers pages and applicant-tracking systems, and presents you a feed
of those postings that you can filter by title, company, location, and workplace type,
with short automated notes on how a posting relates to the profile you supply.


The Service's source code is published under the GNU Affero General Public License v3.0.
This policy describes the behavior of the code as deployed by us; anyone else running
their own copy is a separate controller whose practices we do not govern.

Your use of the Service is also governed by our [Terms of Service](/terms), available at
https://jobcannon.dev/terms.


## 2. The short version

- We collect what you type into the profile picker, the account details Clerk sends us
  when you sign up, and which postings you save, dismiss, or click apply on.

- Our own application sets one cookie, and it lasts only for your browser session. Clerk,
  our sign-in provider, sets its own strictly necessary sign-in cookies once you reach a
  page that requires sign-in — including the sign-in prompt itself — never on our public
  pages: this policy (`/privacy`), our terms of service (`/terms`), the profile picker
  (`/start`), the pre-signup preview (`/preview`), and the sample feed at `/demo` (not the
  personalized feed shown after you sign in, which does load Clerk's script — the cookies
  themselves are described in §10). We set no advertising or tracking cookies.


- **Analytics are off by default.** If you do not opt in, we record no behavioral
  events at all — not in our own database, and not with any analytics provider.

- We do not collect your name, your resume, or your IP address in our application code.

- We do not send your profile or your activity to any external AI or large-language-model
  provider. The only model we run computes text embeddings of job-listing text on our
  own infrastructure — see §3.5.

- You can **export your data** as a JSON download and **delete your account** directly
  in the Service. See §8 (retention and deletion) and §9 (your rights, including export).


## 3. What we collect

### 3.1 Information you give us

| What | When | Where it goes |
|---|---|---|
| Your career profile: skills, target job titles, seniority level, and years of experience | When you complete the profile picker | Stored in our database against your account  |
| Your picker selections before you have an account (titles, companies, skills, seniority, years, workplace type) | While you are using the picker as a visitor | Held in the session cookie only. When you sign up, your titles, skills, seniority, and years are written to your profile; your company and workplace-type selections are used only for the pre-signup preview and are not retained  |
| Whether you granted or declined analytics consent, when, and which consent version | When you answer the consent prompt | Stored against your account, plus an audit record  |

### 3.2 Information Clerk gives us

We use Clerk for sign-up and sign-in. When you create or update an account, Clerk sends
us your **user identifier and email address**. That is all we receive and store from
Clerk.


Your password, your sign-in device details, and your IP address are handled by Clerk — on
Clerk's own hosted sign-in pages, and through Clerk's script, which our sign-in prompt and
every page shown to a signed-in visitor loads (never our public pages) and which contacts
Clerk's servers whenever it does — and are **never seen by our servers**. Our application
verifies your session locally using a cryptographic signature check and makes no network
call to Clerk on a request-by-request basis.


### 3.3 Information generated as you use the Service

| What | Collected when |
|---|---|
| Which postings you saved, dismissed, clicked apply on, or undid an apply on | Always — this is the product working; saving a posting is what a saved posting is   |
| Behavioral analytics events — which postings were shown to you, their position in your feed, the ranking version used, and the surface you were on | **Only if you have granted analytics consent.** Without consent these are discarded before anything is written or sent  |
| A signup record noting how you arrived: the channel, the **hostname** of the site that referred you (never the full address, path, or query), and the signup wave | At signup  |
| A session identifier | On your first contact with the Service; held in the cookie only and not written to our database  |
| A feed session identifier | On your first contact with the Service; held in the cookie and recorded on your signup record, so it appears in your data export. It is not recorded on the individual activity records (postings shown, saved, dismissed, or applied to)   |

**Before you have an account.** If you use the picker as a visitor, a provisional account
and profile record is created for you server-side so your selections can be carried into a
real account if you sign up. If you never sign up, that provisional record is deleted on
the schedule in §8.


### 3.4 What we do not collect

We do not collect your name, a resume or CV, free-text job titles beyond the ones
offered in the picker, or your IP address. No part of our application code reads,
stores, or logs IP addresses.


Note that our hosting provider operates its own infrastructure layer beneath our
application; platform-level request logging there is outside our application code.
See §6.3.

### 3.5 We do not use AI on your data

Job postings in our corpus have embeddings computed by a similarity model that runs **on
our own infrastructure**, and it processes posting text — shared job listings — not your
personal data. No external AI or inference provider receives your profile, your activity,
or your job data.


The short explanatory notes shown beside a posting (why it appeared for you) are
generated by restating fields already stored on that posting. They involve no model call.


## 4. Why we process it, and on what legal basis


| Purpose | Data | Legal basis |
|---|---|---|
| Create and maintain your account | Clerk user id, email | Performance of a contract |
| Build and filter your job feed, and generate the automated notes shown beside postings | Career profile, picker selections | Performance of a contract  |
| Remember what you saved, dismissed, or applied to | Saved/dismissed/applied state | Performance of a contract |
| Keep you signed in and keep the session working | Session cookie contents | Strictly necessary / legitimate interests |
| Understand how the product is used and improve it | Behavioral analytics events | **Consent** — and only consent; nothing is collected without it  |
| Record that you made a consent choice | Consent decision, timestamp, version | Legal obligation — GDPR Art. 6(1)(c), because Art. 7(1) requires being able to demonstrate that a data subject has consented  |

Providing your Clerk user id and email is required to create and use an account; without
them, you cannot use the Service. Analytics data is never required, and declining it does
not affect your use of the Service.


## 5. Analytics and your consent

**Analytics consent is off by default.** A new account starts with consent set to false.


If you have **not** granted consent, behavioral events — postings shown to you, postings
saved, postings dismissed, apply clicks, apply undos — are **discarded outright**. No
record is written to our database and no call is made to our analytics provider. Nothing
is shared with anyone, because nothing is collected without your consent.


Two records are written regardless of your consent choice: the fact that you signed up,
and the fact that you made a consent decision. Neither is forwarded to our analytics
provider unless you granted consent.


You can change your choice at any time on the consent page.


Each consent decision is recorded against a version number for the consent text. If we
change what analytics collect, we change that version, and a consent given under an
earlier version stops authorizing collection until you make a new choice.


**What withdrawing consent does and does not do.** Withdrawing consent stops future
collection. It does **not** immediately delete analytics events already collected, and
withdrawing consent alone does not trigger a deletion request to our analytics
provider — deleting your account does (see §8). Events already recorded are removed by
the scheduled expiry described in §8 (one year after they were recorded) or when your
account is deleted, whichever comes first.


## 6. Who else processes your data

### 6.1 Clerk — accounts and sign-in

Clerk handles sign-up, sign-in, and account management. It holds your credentials and
sees your device and network details on its own pages and, because our sign-in prompt and
every page shown to a signed-in visitor load Clerk's script, on those pages of the Service
too — never on our public pages. It passes us your user id and email. This is required
for the Service to function and is not an optional analytics choice.


### 6.2 PostHog — product analytics (only with your consent)

If, and only if, you have granted analytics consent, we send events to PostHog, routed to
PostHog's **European Union** endpoint (`eu.i.posthog.com`).


What PostHog receives is narrower than what we store ourselves. For each event PostHog
gets:

- a **pseudonymous analytics identifier** — a one-way keyed hash of your account
  identifier, derived on our server with a dedicated secret. It is stable for your
  account (so usage patterns can be analyzed) but PostHog never receives your actual
  account identifier, and the hash cannot be reversed without our server's secret. If
  that secret is not configured, analytics sending is disabled entirely rather than
  falling back to the real identifier

- the name of the event
- a small properties bag for that event — for example, an Apply click carries the
  **hostname** of the site the Apply link leads to (an employer's careers site or
  applicant-tracking system), never the full address


PostHog does **not** receive the posting identifier, your position in the feed, the
ranking version, your feed session identifier, or experiment assignment — those are stored
only in our own database. For example, a "posting shown" event reaches PostHog as nothing
more than a surface label.


### 6.3 Render — hosting

The Service and its single database run on Render. Render therefore processes all
application traffic and holds the database in which the account, profile, and activity
data described in §3 is stored.


Render operates platform-level infrastructure beneath our application. Our application
code configures no request or access logging and records no IP addresses. Render may
apply platform-level request logging as part of operating its infrastructure; any such
logging is governed by Render's own privacy policy and data processing agreement, not by
this policy.


### 6.4 Nobody else

No other third party receives personal data from our servers at runtime. When your
browser loads any page, it fetches two open-source front-end libraries directly from
public content-delivery networks — Tailwind CSS from `cdn.tailwindcss.com` and htmx from
`unpkg.com`. When it loads our sign-in prompt or a page shown to a signed-in visitor, it
also fetches Clerk's sign-in script from `clerk.jobcannon.dev` (§6.1); our public pages do
not. In the ordinary course of serving those files, each of those hosts receives your IP
address and the browser details that accompany any web request, under its own privacy
policy. We send them nothing about you, and they receive nothing else from us.


## 7. International transfers

Analytics events, when consent is granted, are routed to PostHog's European Union
endpoint.


The hosting region of our database is Oregon, USA. For any transfer to Render's
infrastructure we rely on Standard Contractual Clauses under Render's Data Processing
Addendum.

Clerk's Data Processing Addendum states it hosts data primarily on Google Cloud and
Cloudflare infrastructure with no fixed regional restriction — Clerk or its
subprocessors may process data anywhere they maintain facilities. Clerk is a certified
participant in the EU-U.S. Data Privacy Framework and relies on it for such transfers,
falling back to Standard Contractual Clauses where the Framework does not apply.


## 8. How long we keep it

| What | How long |
|---|---|
| Your account, profile, saved postings, and pipeline status | Until your account is deleted. We do not currently expire or prune this data on any schedule  |
| Behavioral analytics events tied to your account (postings shown to you, saved, dismissed, apply clicks) | Deleted automatically **one year (365 days)** after they were recorded, by a daily cleanup job — or earlier, when your account is deleted  |
| Your consent decisions and the record of how you signed up | Until your account is deleted. These are the audit trail for your consent and are not subject to the one-year sweep  |
| The provisional account and profile records of a visitor who browsed but never created an account | The account and profile records are deleted automatically after **30 days** by a daily cleanup job. That sweep and the one-year analytics-event sweep above are the only scheduled expiries we operate  |
| The session cookie | Until you close your browser  |

When your account is deleted, the deletion is a genuine hard delete: your account row is
removed and every dependent record we hold — profile, saved postings, pipeline status,
analytics events — is removed with it by database cascade. This is not a soft flag or an
archival copy. The cascade runs when Clerk, our identity provider, confirms the account
deletion to us, which normally follows your request shortly.

When your account is deleted, we also submit a deletion request to PostHog for the
pseudonymous analytics person described in §6.2, including their previously recorded
events, using that same pseudonymous identifier. PostHog processes deletion requests
asynchronously, so removal from PostHog's systems may not be immediate.


## 9. Your rights

Depending on where you live, you may have rights to access, correct, delete, port, or
restrict the processing of your personal data, and to object to it or withdraw consent.
Here is how each works today:

| Right | How it works today |
|---|---|
| **Withdraw or grant analytics consent** | Fully self-service on the consent page, at any time  |
| **Correct your profile** | Your profile is set from the picker selections carried in when you sign up; there is currently no in-Service way to edit it afterward. Email hello@jobcannon.dev to correct it and we will action the request within 30 days (see below)  |
| **Delete your account and all associated data** | **Self-service** — a "Delete account" link in the Service's footer takes you to a confirmation page; confirming tells Clerk (our identity provider) to delete your account, and when Clerk confirms that deletion to us the same cascade removes every dependent record we hold — profile, saved postings, pipeline status, analytics events. We also submit a deletion request to PostHog for your pseudonymous analytics person and previously recorded events at the same time (see §8). You can also delete your account directly through Clerk's own account management at https://accounts.jobcannon.dev/user; either path triggers the same cascade described above. Clerk, our identity processor, may retain minimal records of your account (for example, logs) after this cascade runs where its own legal or regulatory obligations require it; that retention is governed by Clerk's privacy policy, not this one   |
| **Export or download your data** | **Self-service** — the "Export your data" link in the Service's footer downloads a JSON file containing your profile, saved postings, pipeline status, consent decisions, and analytics events  |
| **See a history of your consent decisions** | Included in the export above — every consent decision you have recorded appears in its events list, and the most recent one is also summarized. There is no separate history page  |

For anything these self-service surfaces do not cover, email hello@jobcannon.dev and we
will fulfill the request manually within 30 days.


If you wish to raise a concern, contact us at hello@jobcannon.dev. If you are in the
European Economic Area, the UK, or Switzerland, you also have the right to lodge a
complaint with the data protection supervisory authority in your own country of
residence — you are not required to go through us, and we do not designate a single
authority for this purpose. We have not appointed an EU representative under GDPR
Article 27. The Service is directed at, and offered to, users in the United States; we
do not market to or otherwise deliberately target users in the EU/EEA, the UK, or
Switzerland. If that changes and we begin directing the Service at users in those
regions, we will revisit whether an EU representative is required.


## 10. Cookies

Our own application sets **one cookie**: a session cookie. Clerk, our sign-in provider,
sets its own sign-in cookies once you reach a page that requires sign-in — including the
sign-in prompt itself; those are described at the end of this section.


It lasts for your browser session and is cleared when your browser session ends. It is
marked `HttpOnly` and `SameSite=Lax` and is sent only over HTTPS.


It holds: your session and feed-session identifiers; how you arrived at the Service
(channel, referring **hostname**, signup wave); the picker selections you have made but
not yet saved to an account; and a flag noting your signup has been processed.


The cookie is **cryptographically signed but not encrypted**. This means we can detect if
it has been tampered with, but its contents are readable by anyone with access to your
browser. This matters because the cookie holds your picker selections before you have an
account. It never contains a password or a credential.


**Sign-in cookies set by Clerk.** The pages of the Service that require sign-in — our
sign-in prompt itself, and every page shown to a signed-in visitor — load Clerk's sign-in
script from `clerk.jobcannon.dev` (§6.1); our public pages (listed in §2) do not. Once you
reach one of those pages — whether or not you go on to sign in — that script sets
`__client_uat` on `jobcannon.dev` and its subdomains, a timestamp Clerk uses to know
whether a sign-in is active (it holds `0` until you sign in), and Clerk's host sets
`__client`, an identifier for your browser's Clerk client, together with Cloudflare
bot-protection cookies (`__cf_bm`, `_cfuvid`) from the network that serves Clerk. Once you
sign in, Clerk adds `__session`, which holds your short-lived, signed sign-in token and is
refreshed by Clerk's script while a page is open, and `clerk_active_context`, which
records which account is active in this browser; Clerk's own sign-in pages
(`accounts.jobcannon.dev`) carry the same kinds of cookies. These cookies are strictly
necessary for sign-in, are described in Clerk's privacy policy, and are not used for
advertising or tracking.


We set no advertising or tracking cookies.


## 11. Security

Your session is authenticated by a signed token that our servers verify cryptographically
on each request.

The session cookie is HTTP-only, same-site, and HTTPS-only in production.

The signing key is a required configuration value and the Service refuses to start
without it.


No method of transmission or storage is perfectly secure, and we cannot guarantee
absolute security.

## 12. Children

The Service is not directed to children and is not intended for anyone under 16. We do
not knowingly collect data from children.

## 13. Changes to this policy

If we change this policy we will update the "last updated" date above and post the
revised version in the Service. We do not currently operate an email notification
system, so we will not promise to email you about changes.


## 14. Applicable law

This policy is interpreted under the laws of California. That does not limit
any mandatory data-protection right you have under the law of the country where you live.

## 15. Contact

Senkichi, LLC

hello@jobcannon.dev
