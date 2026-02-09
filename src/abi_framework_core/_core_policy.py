from __future__ import annotations

from ._core_base import *  # noqa: F401,F403
from ._core_compare import *  # noqa: F401,F403

def normalize_policy_rules(raw_rules: Any, label: str) -> list[PolicyRule]:
    if raw_rules is None:
        return []
    if not isinstance(raw_rules, list):
        raise AbiFrameworkError(f"{label}.rules must be an array when specified")

    out: list[PolicyRule] = []
    for idx, item in enumerate(raw_rules):
        if not isinstance(item, dict):
            raise AbiFrameworkError(f"{label}.rules[{idx}] must be an object")
        rule_id = str(item.get("id") or f"rule_{idx}")
        if not rule_id:
            raise AbiFrameworkError(f"{label}.rules[{idx}].id must be non-empty")
        enabled = bool(item.get("enabled", True))
        severity = str(item.get("severity", "error")).strip().lower()
        if severity not in {"error", "warning"}:
            raise AbiFrameworkError(f"{label}.rules[{idx}].severity must be error or warning")
        message = str(item.get("message") or f"Policy rule violated: {rule_id}")
        when = item.get("when")
        if when is None:
            when = {}
        if not isinstance(when, dict):
            raise AbiFrameworkError(f"{label}.rules[{idx}].when must be an object")
        out.append(
            PolicyRule(
                rule_id=rule_id,
                enabled=enabled,
                severity=severity,
                message=message,
                when=when,
            )
        )
    return out


def normalize_waiver_requirements(
    raw_requirements: Any,
    label: str,
    base_requirements: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = (
        dict(base_requirements)
        if isinstance(base_requirements, dict)
        else dict(DEFAULT_WAIVER_REQUIREMENTS)
    )
    if raw_requirements is None:
        return out
    if not isinstance(raw_requirements, dict):
        raise AbiFrameworkError(f"{label}.waiver_requirements must be an object when specified")
    for key in [
        "require_owner",
        "require_reason",
        "require_expires_utc",
        "require_approved_by",
        "require_ticket",
    ]:
        value = raw_requirements.get(key)
        if value is not None:
            if not isinstance(value, bool):
                raise AbiFrameworkError(f"{label}.waiver_requirements.{key} must be boolean when specified")
            out[key] = value
    for key in ["max_ttl_days", "warn_expiring_within_days"]:
        value = raw_requirements.get(key)
        if value is not None:
            if not isinstance(value, int) or value < 0:
                raise AbiFrameworkError(f"{label}.waiver_requirements.{key} must be non-negative integer when specified")
            out[key] = value
    return out


def normalize_policy_waivers(
    raw_waivers: Any,
    label: str,
    waiver_requirements: dict[str, Any] | None = None,
) -> list[PolicyWaiver]:
    if raw_waivers is None:
        return []
    if not isinstance(raw_waivers, list):
        raise AbiFrameworkError(f"{label}.waivers must be an array when specified")
    requirements = normalize_waiver_requirements(waiver_requirements, label)

    out: list[PolicyWaiver] = []
    for idx, item in enumerate(raw_waivers):
        if not isinstance(item, dict):
            raise AbiFrameworkError(f"{label}.waivers[{idx}] must be an object")
        if not bool(item.get("enabled", True)):
            continue

        waiver_id = str(item.get("id") or f"waiver_{idx}")
        if not waiver_id:
            raise AbiFrameworkError(f"{label}.waivers[{idx}].id must be non-empty")

        severity = str(item.get("severity", "any")).strip().lower()
        if severity not in {"any", "error", "warning"}:
            raise AbiFrameworkError(f"{label}.waivers[{idx}].severity must be any/error/warning")

        pattern_text = str(item.get("pattern") or "")
        if not pattern_text:
            raise AbiFrameworkError(f"{label}.waivers[{idx}].pattern must be non-empty")
        try:
            pattern = re.compile(pattern_text)
        except re.error as exc:
            raise AbiFrameworkError(
                f"{label}.waivers[{idx}].pattern is invalid regex: {pattern_text} ({exc})"
            ) from exc

        targets_raw = item.get("targets")
        target_patterns: tuple[re.Pattern[str], ...]
        if targets_raw is None:
            target_patterns = (re.compile(r".*"),)
        else:
            if not isinstance(targets_raw, list) or not targets_raw:
                raise AbiFrameworkError(f"{label}.waivers[{idx}].targets must be a non-empty array when specified")
            compiled: list[re.Pattern[str]] = []
            for target_idx, target_pattern in enumerate(targets_raw):
                if not isinstance(target_pattern, str) or not target_pattern:
                    raise AbiFrameworkError(
                        f"{label}.waivers[{idx}].targets[{target_idx}] must be a non-empty regex string"
                    )
                try:
                    compiled.append(re.compile(target_pattern))
                except re.error as exc:
                    raise AbiFrameworkError(
                        f"{label}.waivers[{idx}].targets[{target_idx}] invalid regex: {target_pattern} ({exc})"
                    ) from exc
            target_patterns = tuple(compiled)

        expires_utc_raw = item.get("expires_utc")
        expires_utc = None
        if expires_utc_raw is not None:
            if not isinstance(expires_utc_raw, str) or not expires_utc_raw:
                raise AbiFrameworkError(f"{label}.waivers[{idx}].expires_utc must be non-empty ISO string")
            try:
                _ = parse_utc_timestamp(expires_utc_raw)
            except Exception as exc:
                raise AbiFrameworkError(
                    f"{label}.waivers[{idx}].expires_utc invalid ISO timestamp: {expires_utc_raw}"
                ) from exc
            expires_utc = expires_utc_raw

        created_utc_raw = item.get("created_utc")
        created_utc = None
        if created_utc_raw is not None:
            if not isinstance(created_utc_raw, str) or not created_utc_raw:
                raise AbiFrameworkError(f"{label}.waivers[{idx}].created_utc must be non-empty ISO string")
            try:
                _ = parse_utc_timestamp(created_utc_raw)
            except Exception as exc:
                raise AbiFrameworkError(
                    f"{label}.waivers[{idx}].created_utc invalid ISO timestamp: {created_utc_raw}"
                ) from exc
            created_utc = created_utc_raw

        owner = item.get("owner")
        reason = item.get("reason")
        approved_by = item.get("approved_by")
        ticket = item.get("ticket")
        owner_value = str(owner) if isinstance(owner, str) and owner else None
        reason_value = str(reason) if isinstance(reason, str) and reason else None
        approved_by_value = str(approved_by) if isinstance(approved_by, str) and approved_by else None
        ticket_value = str(ticket) if isinstance(ticket, str) and ticket else None

        if bool(requirements.get("require_owner")) and not owner_value:
            raise AbiFrameworkError(f"{label}.waivers[{idx}].owner is required by waiver_requirements")
        if bool(requirements.get("require_reason")) and not reason_value:
            raise AbiFrameworkError(f"{label}.waivers[{idx}].reason is required by waiver_requirements")
        if bool(requirements.get("require_expires_utc")) and not expires_utc:
            raise AbiFrameworkError(f"{label}.waivers[{idx}].expires_utc is required by waiver_requirements")
        if bool(requirements.get("require_approved_by")) and not approved_by_value:
            raise AbiFrameworkError(f"{label}.waivers[{idx}].approved_by is required by waiver_requirements")
        if bool(requirements.get("require_ticket")) and not ticket_value:
            raise AbiFrameworkError(f"{label}.waivers[{idx}].ticket is required by waiver_requirements")

        max_ttl_days = requirements.get("max_ttl_days")
        if isinstance(max_ttl_days, int):
            if not created_utc or not expires_utc:
                raise AbiFrameworkError(
                    f"{label}.waivers[{idx}] must include created_utc and expires_utc when max_ttl_days is configured"
                )
            created_at = parse_utc_timestamp(created_utc)
            expires_at = parse_utc_timestamp(expires_utc)
            ttl_days = (expires_at - created_at).total_seconds() / 86400.0
            if ttl_days < 0:
                raise AbiFrameworkError(f"{label}.waivers[{idx}] expires_utc is earlier than created_utc")
            if ttl_days > float(max_ttl_days):
                raise AbiFrameworkError(
                    f"{label}.waivers[{idx}] TTL is {ttl_days:.2f} days and exceeds max_ttl_days={max_ttl_days}"
                )

        out.append(
            PolicyWaiver(
                waiver_id=waiver_id,
                target_patterns=target_patterns,
                severity=severity,
                message_pattern=pattern,
                expires_utc=expires_utc,
                created_utc=created_utc,
                owner=owner_value,
                reason=reason_value,
                approved_by=approved_by_value,
                ticket=ticket_value,
            )
        )

    return out


def _rule_match_any(patterns: list[re.Pattern[str]], values: list[str]) -> bool:
    if not patterns:
        return True
    for pattern in patterns:
        for value in values:
            if pattern.search(value):
                return True
    return False


def _rule_match_all(patterns: list[re.Pattern[str]], values: list[str]) -> bool:
    if not patterns:
        return True
    for pattern in patterns:
        if not any(pattern.search(value) for value in values):
            return False
    return True


def _to_regex_list(raw: Any, label: str) -> list[re.Pattern[str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AbiFrameworkError(f"{label} must be an array when specified")
    out: list[re.Pattern[str]] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, str) or not item:
            raise AbiFrameworkError(f"{label}[{idx}] must be a non-empty regex string")
        try:
            out.append(re.compile(item))
        except re.error as exc:
            raise AbiFrameworkError(f"{label}[{idx}] invalid regex: {item} ({exc})") from exc
    return out


def _apply_policy_rules(report: dict[str, Any], rules: list[PolicyRule], target_name: str) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    errors = get_message_list(report, "errors")
    warnings = get_message_list(report, "warnings")
    applied: list[dict[str, Any]] = []

    for rule in rules:
        if not rule.enabled:
            continue

        when = rule.when
        classification = str(report.get("change_classification", "none"))

        classification_in = when.get("classification_in")
        if classification_in is not None:
            if not isinstance(classification_in, list) or not all(isinstance(item, str) for item in classification_in):
                raise AbiFrameworkError(f"policy rule '{rule.rule_id}': when.classification_in must be array of strings")
            if classification not in classification_in:
                continue

        classification_not_in = when.get("classification_not_in")
        if classification_not_in is not None:
            if not isinstance(classification_not_in, list) or not all(isinstance(item, str) for item in classification_not_in):
                raise AbiFrameworkError(f"policy rule '{rule.rule_id}': when.classification_not_in must be array of strings")
            if classification in classification_not_in:
                continue

        removed_symbols = get_message_list(report, "removed_symbols")
        added_symbols = get_message_list(report, "added_symbols")
        changed_signatures = get_message_list(report, "changed_signatures")
        breaking_reasons = get_message_list(report, "breaking_reasons")
        additive_reasons = get_message_list(report, "additive_reasons")

        count_checks = [
            ("removed_symbols_count_gt", len(removed_symbols)),
            ("added_symbols_count_gt", len(added_symbols)),
            ("changed_signatures_count_gt", len(changed_signatures)),
            ("breaking_reasons_count_gt", len(breaking_reasons)),
            ("additive_reasons_count_gt", len(additive_reasons)),
            ("warnings_count_gt", len(warnings)),
            ("errors_count_gt", len(errors)),
        ]
        failed_count_gate = False
        for key, current_count in count_checks:
            raw_threshold = when.get(key)
            if raw_threshold is None:
                continue
            if not isinstance(raw_threshold, int):
                raise AbiFrameworkError(f"policy rule '{rule.rule_id}': when.{key} must be integer")
            if current_count <= raw_threshold:
                failed_count_gate = True
                break
        if failed_count_gate:
            continue

        regex_checks: list[tuple[str, list[str], str]] = [
            ("removed_symbols_regex_all", removed_symbols, "all"),
            ("added_symbols_regex_all", added_symbols, "all"),
            ("changed_signatures_regex_all", changed_signatures, "all"),
            ("breaking_reasons_regex_all", breaking_reasons, "all"),
            ("additive_reasons_regex_all", additive_reasons, "all"),
            ("warnings_regex_all", warnings, "all"),
            ("errors_regex_all", errors, "all"),
            ("removed_symbols_regex_any", removed_symbols, "any"),
            ("added_symbols_regex_any", added_symbols, "any"),
            ("changed_signatures_regex_any", changed_signatures, "any"),
            ("breaking_reasons_regex_any", breaking_reasons, "any"),
            ("additive_reasons_regex_any", additive_reasons, "any"),
            ("warnings_regex_any", warnings, "any"),
            ("errors_regex_any", errors, "any"),
        ]
        regex_gate_failed = False
        for key, values, mode in regex_checks:
            raw_patterns = when.get(key)
            if raw_patterns is None:
                continue
            patterns = _to_regex_list(raw_patterns, f"policy rule '{rule.rule_id}' when.{key}")
            if mode == "all":
                if not _rule_match_all(patterns, values):
                    regex_gate_failed = True
                    break
            else:
                if not _rule_match_any(patterns, values):
                    regex_gate_failed = True
                    break
        if regex_gate_failed:
            continue

        message = f"[policy:{rule.rule_id}] {rule.message} (target={target_name})"
        if rule.severity == "warning":
            warnings.append(message)
        else:
            errors.append(message)
        applied.append(
            {
                "id": rule.rule_id,
                "severity": rule.severity,
                "message": message,
            }
        )

    return errors, warnings, applied


def _apply_policy_waivers(
    *,
    target_name: str,
    errors: list[str],
    warnings: list[str],
    waivers: list[PolicyWaiver],
) -> tuple[list[str], list[str], list[dict[str, Any]], list[str]]:
    now = now_utc()
    waived_entries: list[dict[str, Any]] = []
    waiver_warnings: list[str] = []

    def _matches_target(waiver: PolicyWaiver) -> bool:
        return any(pattern.search(target_name) for pattern in waiver.target_patterns)

    def _is_expired(waiver: PolicyWaiver) -> bool:
        if not waiver.expires_utc:
            return False
        try:
            return parse_utc_timestamp(waiver.expires_utc) < now
        except Exception:
            return False

    def _apply_bucket(values: list[str], severity: str) -> list[str]:
        kept: list[str] = []
        for message in values:
            matched = False
            for waiver in waivers:
                if waiver.severity not in {"any", severity}:
                    continue
                if not _matches_target(waiver):
                    continue
                if not waiver.message_pattern.search(message):
                    continue
                if _is_expired(waiver):
                    waiver_warnings.append(
                        f"waiver '{waiver.waiver_id}' expired at {waiver.expires_utc} for target '{target_name}'"
                    )
                    continue
                waived_entries.append(
                    {
                        "waiver_id": waiver.waiver_id,
                        "severity": severity,
                        "message": message,
                        "created_utc": waiver.created_utc,
                        "owner": waiver.owner,
                        "approved_by": waiver.approved_by,
                        "ticket": waiver.ticket,
                        "reason": waiver.reason,
                        "expires_utc": waiver.expires_utc,
                    }
                )
                matched = True
                break
            if not matched:
                kept.append(message)
        return kept

    kept_errors = _apply_bucket(errors, "error")
    kept_warnings = _apply_bucket(warnings, "warning")
    return kept_errors, kept_warnings, waived_entries, waiver_warnings


def resolve_effective_policy(config: dict[str, Any], target_name: str) -> dict[str, Any]:
    defaults = {
        "max_allowed_classification": "breaking",
        "fail_on_warnings": False,
        "require_layout_probe": False,
        "waiver_requirements": dict(DEFAULT_WAIVER_REQUIREMENTS),
        "rules": [],
        "waivers": [],
    }

    root_policy = config.get("policy")
    if isinstance(root_policy, dict):
        for key in ["max_allowed_classification", "fail_on_warnings", "require_layout_probe"]:
            if key in root_policy:
                defaults[key] = root_policy[key]
        defaults["waiver_requirements"] = normalize_waiver_requirements(
            root_policy.get("waiver_requirements"),
            "config.policy",
        )
        defaults["rules"] = normalize_policy_rules(root_policy.get("rules"), "config.policy")
        defaults["waivers"] = normalize_policy_waivers(
            root_policy.get("waivers"),
            "config.policy",
            defaults["waiver_requirements"],
        )

    target = resolve_target(config, target_name)
    target_policy = target.get("policy")
    if isinstance(target_policy, dict):
        for key in ["max_allowed_classification", "fail_on_warnings", "require_layout_probe"]:
            if key in target_policy:
                defaults[key] = target_policy[key]
        effective_requirements = normalize_waiver_requirements(
            target_policy.get("waiver_requirements"),
            f"target '{target_name}'.policy",
            defaults.get("waiver_requirements"),
        )
        defaults["waiver_requirements"] = effective_requirements
        target_rules = normalize_policy_rules(target_policy.get("rules"), f"target '{target_name}'.policy")
        target_waivers = normalize_policy_waivers(
            target_policy.get("waivers"),
            f"target '{target_name}'.policy",
            defaults["waiver_requirements"],
        )
    else:
        target_rules = []
        target_waivers = []

    max_allowed = str(defaults.get("max_allowed_classification", "breaking"))
    if max_allowed not in CLASSIFICATION_ORDER:
        raise AbiFrameworkError(
            f"Invalid policy.max_allowed_classification for target '{target_name}': {max_allowed}"
        )
    return {
        "max_allowed_classification": max_allowed,
        "fail_on_warnings": bool(defaults.get("fail_on_warnings", False)),
        "require_layout_probe": bool(defaults.get("require_layout_probe", False)),
        "waiver_requirements": defaults.get("waiver_requirements"),
        "rules": [*defaults.get("rules", []), *target_rules],
        "waivers": [*defaults.get("waivers", []), *target_waivers],
    }


def apply_policy_to_report(report: dict[str, Any], policy: dict[str, Any], target_name: str) -> dict[str, Any]:
    out = json.loads(json.dumps(report))

    errors = get_message_list(out, "errors")
    warnings = get_message_list(out, "warnings")

    observed = str(out.get("change_classification", "none"))
    max_allowed = str(policy.get("max_allowed_classification", "breaking"))
    if observed not in CLASSIFICATION_ORDER:
        observed = "breaking"
    if max_allowed not in CLASSIFICATION_ORDER:
        max_allowed = "breaking"
    if CLASSIFICATION_ORDER[observed] > CLASSIFICATION_ORDER[max_allowed]:
        errors.append(
            f"Policy violation for target '{target_name}': classification '{observed}' exceeds allowed '{max_allowed}'."
        )

    if bool(policy.get("require_layout_probe", False)):
        layout_diff = out.get("layout_diff")
        layout_available = False
        if isinstance(layout_diff, dict):
            layout_available = bool(layout_diff.get("available_in_current"))
        if not layout_available:
            errors.append(
                f"Policy violation for target '{target_name}': layout probe is required but unavailable."
            )

    policy_rules = policy.get("rules")
    if not isinstance(policy_rules, list):
        policy_rules = []
    typed_rules = [item for item in policy_rules if isinstance(item, PolicyRule)]
    errors, warnings, applied_rules = _apply_policy_rules(
        report=out,
        rules=typed_rules,
        target_name=target_name,
    )

    policy_waivers = policy.get("waivers")
    if not isinstance(policy_waivers, list):
        policy_waivers = []
    typed_waivers = [item for item in policy_waivers if isinstance(item, PolicyWaiver)]
    errors, warnings, applied_waivers, waiver_warnings = _apply_policy_waivers(
        target_name=target_name,
        errors=errors,
        warnings=warnings,
        waivers=typed_waivers,
    )
    warnings.extend(waiver_warnings)

    out["errors"] = errors
    out["warnings"] = warnings
    out["status"] = "pass" if not errors else "fail"
    out["policy"] = {
        "max_allowed_classification": policy.get("max_allowed_classification"),
        "fail_on_warnings": bool(policy.get("fail_on_warnings", False)),
        "require_layout_probe": bool(policy.get("require_layout_probe", False)),
        "waiver_requirements": policy.get("waiver_requirements"),
        "rule_count": len(typed_rules),
        "waiver_count": len(typed_waivers),
    }
    out["policy_rules_applied"] = applied_rules
    out["waivers_applied"] = applied_waivers
    validate_report_payload(out, f"policy report '{target_name}'")
    return out


