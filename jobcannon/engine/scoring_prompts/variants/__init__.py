"""Phase 4 prompt variants.

Each module in this package is a candidate scoring prompt for the Phase 4
rubric redesign. Loaded by ``jobcannon.engine.job_scorer._resolve_variant_module``
when ``config["scoring"]["prompt_variant"]`` names it.

Required exports per variant:
    V3_SCORING_PROMPT          # aggregate (used by no-context callers)
    FIELD_REINFORCEMENT        # strict field-name block
    FEWSHOT_EXAMPLES           # 1-5 calibration examples
    JOB_ASSESSMENT_SCHEMA      # JSON schema for the dispatcher

Optional:
    V3_SCORING_PROMPT_HEADER   # rubric header alone (used when
                                 candidate_context is spliced in)
"""
