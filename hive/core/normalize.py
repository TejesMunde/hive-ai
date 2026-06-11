"""
Lightweight token normaliser: case-fold + punctuation strip + suffix stem.

Pure stdlib. Not a full Porter — covers ~80% of English noun/verb morphology
relevant to dev decisions (errors→error, monitoring→monitor, tries→try).

Goal: query "track production errors" matches doc "error monitoring".
"""

_STOP = {
    "a", "an", "the", "is", "in", "on", "at", "to", "for", "of", "and",
    "or", "but", "we", "our", "this", "that", "how", "what", "why",
    "where", "when", "are", "was", "be", "by", "as", "it", "its", "i",
    "use", "using", "used", "with", "from", "into", "than", "then",
    "do", "does", "did", "have", "has", "had", "can", "should", "would",
}

_PUNCT = ".,!?;:()[]\"'`<>"


def _strip(word: str) -> str:
    return word.strip(_PUNCT)


def _stem(word: str) -> str:
    """
    Conservative suffix stripping. Only strip if root has >=3 chars left.
    Order matters — longest suffix first.
    """
    if len(word) < 4:
        return word

    # -ies → -y   (companies → company, tries → try)
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"

    # -ied → -y   (tried → try, denied → deny)
    if word.endswith("ied") and len(word) > 4:
        return word[:-3] + "y"

    # -sses → -ss (passes → pass)  — explicitly keep ss
    if word.endswith("sses"):
        return word[:-2]

    # -ion → ''  only when stripping leaves a root ending in ct/ss/pt.
    #   detection → detect, rejection → reject, production → product,
    #   exception → except, encryption → encrypt, session → sess.
    #   Skips: mention → ment, station → stat (root ends in nt/at).
    if word.endswith("ion") and len(word) > 5:
        root = word[:-3]
        if root.endswith(("ct", "ss", "pt")):
            return root

    # -ing → ''   (monitoring → monitor)
    if word.endswith("ing") and len(word) > 5:
        return word[:-3]

    # -edly / -ingly → root
    if word.endswith("edly") and len(word) > 6:
        return word[:-4]
    if word.endswith("ingly") and len(word) > 7:
        return word[:-5]

    # -ed → root, with silent-e correction.
    #   moved → move, saved → save (preceded by 'v')
    #   cached → cache, watched → watche (preceded by 'ch'/'sh'/'th')
    #   existed → exist, tested → test (general case)
    if word.endswith("ed") and len(word) > 4 and word[-3] not in "aeiou":
        before2 = word[-4:-2]
        before1 = word[-3]
        if before1 == "v" or before2 in ("ch", "sh", "th"):
            return word[:-1]   # strip only 'd' — silent-e survives
        return word[:-2]

    # -ly → ''    (quickly → quick)
    if word.endswith("ly") and len(word) > 4:
        return word[:-2]

    # -s → ''     (errors → error) but skip -ss/-us/-is/-os
    if word.endswith("s") and not word.endswith(("ss", "us", "is", "os")) and len(word) > 3:
        return word[:-1]

    return word


# Lightweight dev-term synonym map. Each key, when seen, also emits the
# listed canonical tokens — solves "query says X, doc says Y" mismatches
# for cases where the relationship is unambiguous.
#
# Keep this small. Every entry is a potential false-positive source.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    # Relational stores → "database"
    "sqlite":     ("database",),
    "postgresql": ("database",),
    "postgres":   ("database",),
    "mysql":      ("database",),
    "mariadb":    ("database",),
    "mongodb":    ("database",),
    "datastore":  ("database",),
    "persistence":("database",),
    "persistent": ("database",),
    "oltp":       ("database",),
    "olap":       ("database", "analytics"),
    "duckdb":     ("database", "analytics"),
    # Cache / key-value
    "redis":      ("cache", "key-value"),
    "ephemeral":  ("cache",),
    # Duplicate detection algorithms → "duplicate"
    "jaccard":    ("duplicate",),
    "sequencematcher": ("duplicate",),
    # Dependency vocabulary
    "stdlib":     ("dependency",),
    "pip":        ("dependency", "package"),
    # Web frameworks → "framework" AND "api"
    "fastapi":    ("framework", "api"),
    "flask":      ("framework", "api"),
    "django":     ("framework", "api"),
    "starlette":  ("framework", "api"),
    # Tier vocabulary
    "hot":        ("tier",),
    "warm":       ("tier",),
    "cold":       ("tier",),
    # Observability — monitoring IS tracking
    "monitor":    ("track",),
    "telemetry":  ("track", "observability"),
    "trace":      ("track", "observability"),
    "exception":  ("error",),
    "errors":     ("error",),
    "sentry":     ("error", "observability"),
    "loki":       ("log", "observability"),
    "elasticsearch": ("log", "observability"),
    "prometheus": ("metric", "observability"),
    "mimir":      ("metric", "observability"),
    "opentelemetry": ("track", "observability"),
    # Auth / crypto
    "jwt":        ("auth", "token"),
    "oauth2":     ("auth", "token"),
    "oauth":      ("auth", "token"),
    "pkce":       ("auth", "native"),
    "argon2id":   ("hash", "password", "kdf"),
    "argon2":     ("hash", "password", "kdf"),
    "bcrypt":     ("hash", "password", "kdf"),
    "kdf":        ("hash", "password"),
    "hashing":    ("hash",),
    "credentials":("auth", "token"),
    "httponly":   ("cookie", "token"),
    "cookies":    ("cookie",),
    # Messaging
    "kafka":      ("messaging", "events", "stream"),
    "rabbitmq":   ("messaging",),
    "amqp":       ("messaging",),
    "nats":       ("messaging", "rpc"),
    # Deployment
    "kubernetes": ("deploy", "orchestration"),
    "k8s":        ("deploy", "orchestration", "kubernetes"),
    "helm":       ("deploy", "kubernetes", "manifest"),
    "argocd":     ("deploy", "gitops"),
    "gitops":     ("deploy",),
    "serverless": ("deploy", "lambda"),
    "lambda":     ("deploy", "serverless"),
    "manifest":   ("config",),
    # Frontend / UI
    "react":      ("frontend", "ui"),
    "tailwind":   ("styling", "css", "frontend"),
    "styled":     ("styling", "css"),
    "tanstack":   ("frontend", "fetching"),
    "ssr":        ("frontend", "render"),
    # Data pipeline / ML
    "airflow":    ("orchestration", "workflow"),
    "dbt":        ("transform", "etl"),
    "iceberg":    ("lakehouse", "table"),
    "onnx":       ("inference", "ml"),
    "hnsw":       ("ann", "vector", "index"),
    "ann":        ("vector", "index"),
    "approximate":("ann", "vector"),
    "nearest":    ("ann", "vector"),
    "neighbor":   ("ann", "vector"),
    "embedding":  ("vector", "ml"),
    "encoder":    ("vector", "embedding", "ml"),
    "reranker":   ("retrieval", "ml"),
    "ner":        ("entity", "extraction", "ml"),
    # Testing
    "pytest":     ("test",),
    "hypothesis": ("test", "fuzz"),
    "e2e":        ("test", "browser"),
}


def _expand(token: str) -> tuple[str, ...]:
    extra = _SYNONYMS.get(token)
    return (token,) + extra if extra else (token,)


def normalize_tokens(text: str) -> set[str]:
    """
    Full pipeline: lowercase → strip punctuation → drop stopwords → stem →
    synonym expand. Returns a set (bag) of normalised tokens.

    Synonyms emit canonical tags alongside the original token, never replace
    it. False positives stay bounded to the curated map in _SYNONYMS.
    """
    out: set[str] = set()
    for raw in (text or "").split():
        w = _strip(raw).lower()
        if len(w) <= 2 or w in _STOP:
            continue
        stem = _stem(w)
        for tok in _expand(stem):
            out.add(tok)
    return out
