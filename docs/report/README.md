# Project report

`Udyog_IQ_Project_Report.docx` is the submission document for the Arduino
Physical AI Challenge India 2026, following the official template's section
structure.

## Regenerating it

The report is generated from `tools/build_report.js` rather than hand-edited,
so that a corrected number changes in one place and cannot drift out of sync
with the rest of the repository.

```bash
npm install docx
node tools/build_report.js docs/report/Udyog_IQ_Project_Report.docx
```

## House style

The prose contains no dash characters at all. The only dashes anywhere in the
document sit inside literal identifiers, where removing one would make the name
wrong: the meter part number, the two processor core names, the weather service,
the Python package, and the repository URL. Thirteen in total, all verified by
the audit built into the build. If you edit the text, keep to that rule.

## Still to fill in before submitting

The document marks these in red italics:

- Team name, registration / team ID, institution and city
- Additional team members
- Demo video link (public YouTube or Drive)
- Date and signature
- Three project photographs, in the dashed placeholder boxes

Every quantitative result in the report was produced against the simulator with
known ground truth and is labelled as such. If you re-run any of it on hardware,
update the numbers *and* the labels together.
