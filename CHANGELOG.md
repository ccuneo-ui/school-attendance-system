# Changelog

2026-08-19: Staff editor now exposes a "Scheduler" permission under the People silo, so an admin can grant a specific staff member save/edit access to the scheduler (the checkbox was missing even though the backend already recognized the key). Ticking it lets that person hard-save the live schedule; leaving it off keeps them in the read-only sandbox.

2026-08-19: Scheduler is now open to all signed-in staff as a read-only sandbox — anyone can open it and explore (generate, drag, swap, merge), but for staff without the scheduler permission saving is fully off: the Save button is hidden, autosave is disabled, and the save endpoint rejects them on the server, so nothing they do touches the live schedule. A "view only" banner makes that clear; a refresh returns the live schedule. Editors with the scheduler permission are unchanged. A "Schedule" link now appears in the Reference menu so all staff can find it.

2026-08-18: Site-wide dropdown navigation bar added to every page — Daily Input, Reference, People and Billing menus (permission-filtered) let staff jump between pages directly instead of routing back through the home screen; the old per-page "Home" buttons are now hidden.

2026-08-10: Scheduler — Friday exception to 7th-grade math alignment: 7th Gold may now run at a different period than 7th Blue/White on Fridays (Blue and White stay aligned). This stops McLeod from being booked straight through the Friday lunch window, so he gets a real midday lunch instead of a mislabeled 1st-period one. Verified drafts stay complete.

2026-08-10: Scheduler rule change — Friday Kindergarten PE moved from 3rd period to 7th period (Wednesday stays P3), which frees a 3rd-period slot for Kindergarten Spanish (Murphy teaches Spanish mornings only, so P3 is its only option). Fixes a previously unplaceable Kindergarten Spanish class; drafts now generate complete.

2026-08-10: Scheduler board — the Holding area is now a floating panel pinned to the vertical middle of the right edge (instead of across the top), so you drag a class straight out to the side to park it, never toward the top edge that auto-scrolls the grid; auto-scroll is also paused while the cursor is over it.

2026-08-10: Scheduler master board — you can now combine two split sections into a whole-grade class: drag one half (e.g. 5th Blue Spanish) onto the other half of the same grade, subject, and teacher, and confirm, and that period becomes a single whole-grade block (both halves together). Blocked with a clear message if it would create a conflict; Undo reverses it, and it's saved with the schedule.

2026-08-10: Scheduler — new rule: no STEAM in 7th or 8th period (enforced on generate and manual moves). Master-board fixes: the Holding area now stays pinned on screen while you scroll, and the grid keeps its scroll position when you park a class instead of jumping back to the top.

2026-08-10: Scheduler rules updated — 7th-grade Science is now pinned to periods 7 and 8 every day (7A then 7B, both with Alvarez), and each grade's split math sections (6th, 7th, 8th) are pulled to the same period as that grade's Blue/Duthie section as much as possible, so a grade does math together. Two-preps-a-day and PE/Art division clustering stay as soft preferences. Verified a full conflict-free draft still generates.

2026-08-10: New Scheduler page (People section) hosts the master-schedule generator inside the portal — the same draft-schedule tool, now saved to the server instead of downloaded files. It loads your saved schedule automatically and autosaves as you generate and edit; inputs can still be exported/imported as backup files. (Phase 1: hosting + server save. Live class/roster sync and the per-student view come next.)

2026-08-10: Classes page has a new "Gold & Blue" tab for assigning each student a grade split color (Gold, Blue, or None) and auto-populating rosters from it in one step — Gold students fill every Gold class, Blue every Blue class, and whole-grade/ungrouped classes get the whole grade. Populate overwrites the grade's subject-class rosters (a start-of-year setup action); 7th White math and homeroom/advisory classes are left untouched. Individual rosters remain hand-editable for exceptions.

2026-08-05: Staff permissions are now per-page within each silo. In the staff editor, tick a whole silo ("select all") or individual pages, and page access is enforced on the server (not just hidden on the home screen). Added an "effective superadmin" tier: holders can access every page and manage everyone's permissions, and can promote/demote other effective superadmins. Closed a privilege-escalation gap — changing permissions now requires permission-management authority, no one can change their own permissions, and the superadmin account can't be modified or deleted. Existing staff are auto-migrated from the old three flags so no one loses access (daily-input pages, which were open to all staff, stay that way and can now be restricted per person; billing pages are now properly enforced).

2026-08-05: Fixed an error ("dictionary update sequence element…") when creating a class or editing a roster — the homeroom/advisory sync now reads the section correctly on the write path.

2026-08-05: New Classes page (People section) — create and manage homeroom, advisory, subject, and elective classes in one place: set each class's teacher (active staff only), room, term, and grade, and build its roster (add students filtered by grade, or remove them). Adding/removing students on a homeroom or advisory class also updates that student's homeroom/advisory on their record. New Rooms page manages the room list that feeds the class dropdowns (add, rename, activate/deactivate).

2026-08-04: Groundwork for a unified "classes/sections" model (Phase 1) — new rooms, sections, and section_enrollments tables. A section is one teacher + room + term + roster, unifying homeroom, advisory, subject classes, and electives. No visible change yet; homeroom still works off its existing field. A one-time script (build_sections_from_homeroom.py) seeds homeroom/advisory sections from current assignments.

2026-08-04: Homeroom Attendance Report now has a School Year picker (defaults to the current year) so you can pull prior years on demand; the report, CSV export, per-student detail, and trimester dates all follow the selected year. The subtitle shows the selected year instead of a fixed 2025-26.

2026-08-04: Financial Aid page now opens on the current school year (from calendar settings) even before any families are entered for it, instead of defaulting to the newest year that happens to have data. Backend financial-aid endpoints also fall back to the current year rather than a hard-coded 2025-26.

2026-08-04: Lunch dashboard now defaults its year dropdown to the current school year from the calendar settings (rolls over July 1) instead of always assuming the September changeover — so in summer it opens on the upcoming year rather than the prior one.

2026-08-04: Homeroom attendance report (trimester tally) and the per-student attendance detail now read trimester dates from the School Year calendar settings for the current year, instead of hard-coded dates. The report can also target a prior year with ?school_year=YYYY. Hard-coded 2025-26 dates remain only as a fallback when a year has no dates set.

2026-08-04: School Calendar page now has a School Year setting where admins define when the year rolls over (defaults to July 1) and each year's first/last day and trimester dates. Pages that default to a school year now roll to the new year on July 1 instead of September, so summer shows the upcoming year (fixes the lunch dashboard showing the prior year in July).

2026-07-29: Monthly Billing Report (full-school view) now includes guest students — non-enrolled students who attend programs like tutoring. They appear in their own "Guests" section at the bottom of the report, with a "Guests" filter pill, and are included in the CSV export. Previously the full-school report only showed active/enrolled students, though guests already appeared in the single-student dispute lookup.

2026-07-27: Family Directory now prints a shareable Family Contact Directory (family, address, parents' names/phones/emails), and families can be opted out of it via a checkbox in Family Manager (opted-out families stay visible to staff and on emergency sheets, marked "Unlisted"). Backfilled student emergency contacts from Finalsite.

2026-07-21: New Family Manager page (People silo) — staff with People access can edit household contact info and parents/guardians (phone, email, relationship, pickup authorization), saving to the live records. Student details, including each student's emergency contact, stay in the Student Directory.
2026-07-21: New Family Directory page (Reference silo, read-only, all staff) — search families by family or student name, view full household contact info, parents/guardians with phones and pickup authorization, and each student's emergency contact, and print a per-family Emergency Sheet or a compact all-active-students Emergency Contact Directory.
2026-05-31: New Lunch Billing module — Lunch Dashboard for entering monthly enrollment and pizza counts, lunch rates on the Billing Rates page, and lunch charges now flow into the existing Monthly Billing Report as a per-student "Lunch" line item (column, summary, CSV export, and dispute-lookup detail).
2026-04-26: 1-on-1 Tutoring records now support 30 min / 1 hr duration, with a Duration column and weighted Sessions tile and billing.
