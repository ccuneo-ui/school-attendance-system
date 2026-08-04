# Changelog

2026-08-04: Homeroom attendance report (trimester tally) and the per-student attendance detail now read trimester dates from the School Year calendar settings for the current year, instead of hard-coded dates. The report can also target a prior year with ?school_year=YYYY. Hard-coded 2025-26 dates remain only as a fallback when a year has no dates set.

2026-08-04: School Calendar page now has a School Year setting where admins define when the year rolls over (defaults to July 1) and each year's first/last day and trimester dates. Pages that default to a school year now roll to the new year on July 1 instead of September, so summer shows the upcoming year (fixes the lunch dashboard showing the prior year in July).

2026-07-29: Monthly Billing Report (full-school view) now includes guest students — non-enrolled students who attend programs like tutoring. They appear in their own "Guests" section at the bottom of the report, with a "Guests" filter pill, and are included in the CSV export. Previously the full-school report only showed active/enrolled students, though guests already appeared in the single-student dispute lookup.

2026-07-27: Family Directory now prints a shareable Family Contact Directory (family, address, parents' names/phones/emails), and families can be opted out of it via a checkbox in Family Manager (opted-out families stay visible to staff and on emergency sheets, marked "Unlisted"). Backfilled student emergency contacts from Finalsite.

2026-07-21: New Family Manager page (People silo) — staff with People access can edit household contact info and parents/guardians (phone, email, relationship, pickup authorization), saving to the live records. Student details, including each student's emergency contact, stay in the Student Directory.
2026-07-21: New Family Directory page (Reference silo, read-only, all staff) — search families by family or student name, view full household contact info, parents/guardians with phones and pickup authorization, and each student's emergency contact, and print a per-family Emergency Sheet or a compact all-active-students Emergency Contact Directory.
2026-05-31: New Lunch Billing module — Lunch Dashboard for entering monthly enrollment and pizza counts, lunch rates on the Billing Rates page, and lunch charges now flow into the existing Monthly Billing Report as a per-student "Lunch" line item (column, summary, CSV export, and dispute-lookup detail).
2026-04-26: 1-on-1 Tutoring records now support 30 min / 1 hr duration, with a Duration column and weighted Sessions tile and billing.
