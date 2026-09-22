"""Phase 2, the AWS SCT path -- run the real tool, show its own findings.

`assess/` judges the estate with this project's own 50 rules. This package does
something different: it drives **AWS Schema Conversion Tool** and presents what
SCT itself reports, including the PDF and CSV that SCT writes.

The two are not interchangeable and neither replaces the other:

  assess/   OPS, SEC, DQ, PERF and RDS findings -- NOARCHIVELOG, supplemental
            logging, missing primary keys, grants. SCT does not look at these,
            and Phases 3, 5 and 7 all read `assessment.json` for them.
  sct/      schema and stored-code conversion against a chosen target, graded
            by the work a person must do. This is what a client means by "the
            SCT report", and it is AWS's verdict rather than ours.

**The host is a seam, deliberately.** SCT is a stateful desktop application
driven through a batch CLI, so where it runs is an operational choice and not an
architectural one:

  sct.hosts.local   subprocess on this machine -- the demo, and how the
                    scenario generation and the parser are developed
  sct.hosts.ec2     SSM send_command against an instance in the customer's VPC,
                    where their existing Direct Connect or VPN already reaches
                    their Oracle

Lambda is **not** a host here and the reason is worth keeping written down,
because it looks like it should work: the image would fit (10 GB) and SCT's CLI
is genuinely headless, but Lambda's filesystem is read-only apart from `/tmp`
while SCT writes inside its own install directory, its 15-minute ceiling is hard
where a real customer estate assesses for hours, and baking AWS's licensed
binary into an ECR image is a redistribution question rather than a technical
one. It also cannot reach a local Oracle from inside AWS at all.
"""
