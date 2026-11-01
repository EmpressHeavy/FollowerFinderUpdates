from __future__ import annotations

"""X account-archive follower adapter (STOP POINT A).

Reads a user-supplied ZIP. Uses the ZIP central directory, and a manifest/index
if one is present, only to locate follower-named files. Does not read DMs,
posts, media, ads, contacts, location, or other unrelated members.

The real X archive schema is UNKNOWN. This module does not guess field names.
It copies a field only when the supplied follower object actually has that key
and the value is usable. Missing stays omitted, with the string "Not available"
on an explicit missing map. Missing is never coerced to 0 or false.

Does not call the classifier, the network, or any removal path.
Account handle is always a caller parameter.
""",
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping
from zipfile import BadZipFile, ZipFile

SOURCE = "X_ACCOUNT_ARCHIVE"
NOT_AVAILABLE = "Not available"

# Normalized contract keys. These are adapter outputs, not a verified X schema.
CONTRACT_FIELDS = (
    "source_record_id",
    "x_user_id",
    "handle",
    "display_name",
    "verified",
    "protected",
    "you_follow_them",
    "follows_you",
    "created_at",
    "public_metrics",
    "profile_url",
    "source_timestamp",
)
IDENTITY_FIELDS = ("x_user_id", "handle")
BOOL_FIELDS = ("verified", "protected", "you_follow_them", "follows_you")
STRING_FIELDS = (
    "source_record_id",
    "handle",
    "display_name",
    "created_at",
    "profile_url",
    "source_timestamp",
)
# Compared for delta equality. Archive-level clocks are preserved beside the
# record and are not themselves a follower mutation.
DELTA_COMPARE_FIELDS = (
    "x_user_id",
    "handle",
    "display_name",
    "verified",
    "protected",
    "you_follow_them",
    "follows_you",
    "created_at",
    "public_metrics",
    "profile_url",
)

# Manifest keys that may carry an export time. Which key a real X archive uses
# is UNKNOWN. Only an exact present key is read. Nothing else in the manifest
# is retained.
EXPORT_TIMESTAMP_KEYS = (
    "export_timestamp",
    "generated_at",
    "generatedAt",
    "archive_created_at",
    "createdAt",
)

# Max uncompressed bytes read from one manifest or follower member.
MAX_MEMBER_BYTES = 20_000_000

_FOLLOWER_RE = re.compile(r"(^|/)followers?([/_.-]|$)")
_FOLLOWING_RE = re.compile(r"(^|/)following([/_.-]|$)")
_MANIFEST_RE = re.compile(r"(^|/)manifest([/_.-]|$)")
_DM_RE = re.compile(r"direct[-_ ]?messages?|directmessage|(^|/)dms?([/_.-]|$)")
_TWEET_RE = re.compile(r"(^|/)tweets?([/_.-]|$)")
_AD_RE = re.compile(r"(^|/)ads?([/_.-]|$)")
_PERSONALIZATION_RE = re.compile(r"personalization")
_CONTACT_RE = re.compile(r"address[-_]?book|(^|/)contacts?([/_.-]|$)")
_LOCATION_RE = re.compile(r"(^|/)locations?([/_.-]|$)|ip[-_]?audit")
_MEDIA_RE = re.compile(
    r"(^|/)media(/|$)|[.](mp4|jpg|jpeg|png|gif|webp|mov|mp3)$",
    re.IGNORECASE,
)


def _norm_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def classify_archive_path(path: str) -> str:
    """Classify a ZIP member by path only. Does not look at bytes.

    Sensitive categories are checked before follower so a name that merely
    contains both ideas is not opened.
    """
    name = _norm_path(path).lower()
    if _DM_RE.search(name):
        return "direct_message"
    if _TWEET_RE.search(name):
        return "tweet"
    if _AD_RE.search(name):
        return "ad"
    if _PERSONALIZATION_RE.search(name):
        return "personalization"
    if _CONTACT_RE.search(name):
        return "contact"
    if _LOCATION_RE.search(name):
        return "location"
    if _MEDIA_RE.search(name):
        return "media"
    if _MANIFEST_RE.search(name):
        return "manifest"
    if _FOLLOWER_RE.search(name):
        return "follower"
    if _FOLLOWING_RE.search(name):
        return "following"
    return "unrelated"


def _blocked(
    account_handle: str,
    reason: str,
    message: str,
    *,
    files_accessed: list[str] | None = None,
    files_ignored: list[dict[str, str]] | None = None,
    files_not_accessed: list[str] | None = None,
    observed_follower_keys: list[str] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "block_reason": reason,
        "message": message,
        "account_handle": account_handle,
        "provenance": provenance
        or {
            "source": SOURCE,
            "account_handle": account_handle,
            "account_handle_origin": "caller_parameter",
            "export_timestamp": NOT_AVAILABLE,
            "export_timestamp_key": None,
            "schema": "UNKNOWN",
        },
        "files_accessed": list(files_accessed or []),
        "files_ignored": list(files_ignored or []),
        "files_not_accessed": list(files_not_accessed or []),
        "observed_follower_keys": list(observed_follower_keys or []),
        "records": [],
        "record_count": 0,
    }


def _parse_json_container(raw: bytes) -> Any:
    """Parse JSON, or JSON after a single leading assignment wrapper.

    The wrapper strip is defensive. It is not a claim that X archives use it.
    """
    if len(raw) > MAX_MEMBER_BYTES:
        raise ValueError("member exceeds read limit")
    text = raw.decode("utf-8-sig")
    stripped = text.lstrip()
    if not stripped:
        raise ValueError("empty")
    if stripped[0] in "{[":
        return json.loads(stripped)
    start = None
    for index, char in enumerate(stripped):
        if char in "{[":
            start = index
            break
    if start is None:
        raise ValueError("no json container")
    body = stripped[start:].strip()
    if body.endswith(";"):
        body = body[:-1].rstrip()
    return json.loads(body)


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text and text != NOT_AVAILABLE:
            return text
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _collect_path_strings(value: Any, found: set[str]) -> None:
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 240 or any(ch in text for ch in "\n\r"):
            return
        lower = text.lower()
        if "/" in text or lower.endswith((".js", ".json", ".html")):
            found.add(_norm_path(text))
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_path_strings(item, found)
        return
    if isinstance(value, list):
        for item in value:
            _collect_path_strings(item, found)


def _export_timestamp_from_manifest(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    for key in EXPORT_TIMESTAMP_KEYS:
        if key in value:
            text = _string_or_none(value.get(key))
            if text:
                return text, key
    return None, None


def _public_metrics(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Copy only keys present on the object. Do not fill gaps with 0."""
    if not isinstance(value, dict):
        return None, "present_not_object"
    copied: dict[str, Any] = {}
    for key, item in value.items():
        if item is None:
            continue
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float, str)):
            copied[str(key)] = item
    if not copied:
        return None, "empty_or_null_only"
    return copied, None


def _normalize_object(
    obj: Mapping[str, Any],
    *,
    archive_export_timestamp: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return a record only when this object has an identity key present.

    Keys that are not part of the contract are ignored (not copied). Their
    names are reported by the caller via observed keys.
    """
    observed = [key for key in obj.keys()]
    if not any(key in obj for key in IDENTITY_FIELDS):
        return None, observed

    record: dict[str, Any] = {"source": SOURCE}
    missing: dict[str, str] = {}

    user_id_text = None
    if "x_user_id" in obj:
        user_id_text = _string_or_none(obj.get("x_user_id"))
        if user_id_text and user_id_text.isdigit():
            record["x_user_id"] = user_id_text
        else:
            missing["x_user_id"] = NOT_AVAILABLE
    else:
        missing["x_user_id"] = NOT_AVAILABLE

    for key in STRING_FIELDS:
        if key == "source_record_id" or key == "source_timestamp":
            continue
        if key not in obj:
            missing[key] = NOT_AVAILABLE
            continue
        text = _string_or_none(obj.get(key))
        if text is None:
            missing[key] = NOT_AVAILABLE
        else:
            record[key] = text

    for key in BOOL_FIELDS:
        if key not in obj:
            missing[key] = NOT_AVAILABLE
            continue
        value = obj.get(key)
        if isinstance(value, bool):
            record[key] = value
        else:
            # Present but not a boolean. Do not coerce 0, 1, "false", or null.
            missing[key] = NOT_AVAILABLE

    if "public_metrics" not in obj or obj.get("public_metrics") is None:
        missing["public_metrics"] = NOT_AVAILABLE
    else:
        metrics, _why = _public_metrics(obj.get("public_metrics"))
        if metrics is None:
            missing["public_metrics"] = NOT_AVAILABLE
        else:
            record["public_metrics"] = metrics

    if "source_timestamp" in obj:
        text = _string_or_none(obj.get("source_timestamp"))
        if text is None:
            missing["source_timestamp"] = NOT_AVAILABLE
            record["source_timestamp_origin"] = "missing"
        else:
            record["source_timestamp"] = text
            record["source_timestamp_origin"] = "record"
    elif archive_export_timestamp:
        record["source_timestamp"] = archive_export_timestamp
        record["source_timestamp_origin"] = "archive_export"
    else:
        missing["source_timestamp"] = NOT_AVAILABLE
        record["source_timestamp_origin"] = "missing"

    if "source_record_id" in obj:
        text = _string_or_none(obj.get("source_record_id"))
        if text is None:
            missing["source_record_id"] = NOT_AVAILABLE
        else:
            record["source_record_id"] = text
            record["source_record_id_origin"] = "archive"
    elif "x_user_id" in record:
        record["source_record_id"] = f"x-archive:{record['x_user_id']}"
        record["source_record_id_origin"] = "adapter_from_x_user_id"
    elif "handle" in record:
        record["source_record_id"] = f"x-archive:handle:{record['handle']}"
        record["source_record_id_origin"] = "adapter_from_handle"
    else:
        missing["source_record_id"] = NOT_AVAILABLE

    if "x_user_id" not in record and "handle" not in record:
        return None, observed

    record["missing"] = missing
    record["missing_fields"] = sorted(missing)
    return record, observed


def _record_lists_from_parsed(parsed: Any) -> tuple[list[Any] | None, str | None]:
    """Locate the record list from the parsed value's actual shape.

    Does not try alternate field names. A dict is a record list only when it
    has exactly one list-of-objects value, or several and only one key contains
    'follower'. Otherwise the caller must block.
    """
    if isinstance(parsed, list):
        return parsed, None
    if isinstance(parsed, dict):
        lists = [
            (key, value)
            for key, value in parsed.items()
            if isinstance(value, list)
        ]
        if len(lists) == 1:
            return lists[0][1], str(lists[0][0])
        follower_lists = [
            (key, value) for key, value in lists if "follower" in str(key).lower()
        ]
        if len(follower_lists) == 1:
            return follower_lists[0][1], str(follower_lists[0][0])
        if any(key in parsed for key in IDENTITY_FIELDS):
            return [parsed], None
        return None, "no_single_record_list"
    return None, "unsupported_top_level"


def _observed_keys(records_raw: list[Any]) -> list[str]:
    keys: set[str] = set()
    for item in records_raw:
        if isinstance(item, dict):
            keys.update(str(key) for key in item.keys())
    return sorted(keys)


def _normalize_follower_payload(
    parsed: Any,
    *,
    archive_export_timestamp: str | None,
) -> tuple[str, list[dict[str, Any]], list[str], str]:
    """Returns status, records, observed keys, detail.

    status is 'ok', 'empty', or 'unrecognized'.
    """
    raw_list, detail = _record_lists_from_parsed(parsed)
    if raw_list is None:
        observed: list[str] = []
        if isinstance(parsed, dict):
            observed = sorted(str(key) for key in parsed.keys())
        return "unrecognized", [], observed, detail or "unrecognized"
    if len(raw_list) == 0:
        return "empty", [], [], detail or "empty_list"
    if not all(isinstance(item, dict) for item in raw_list):
        return "unrecognized", [], _observed_keys(raw_list), "non_object_rows"
    observed = _observed_keys(raw_list)
    if not any(key in observed for key in IDENTITY_FIELDS):
        return "unrecognized", [], observed, "no_identity_keys"
    records: list[dict[str, Any]] = []
    for item in raw_list:
        record, _keys = _normalize_object(
            item, archive_export_timestamp=archive_export_timestamp
        )
        if record is not None:
            records.append(record)
    if not records:
        return "unrecognized", [], observed, "identity_keys_unusable"
    return "ok", records, observed, detail or "list"


def _read_member(zf: ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > MAX_MEMBER_BYTES:
        raise ValueError(f"{name} exceeds read limit")
    return zf.read(name)


def load_x_archive_followers(zip_path: str | Path, account_handle: str) -> dict[str, Any]:
    """Load follower records from an account-archive ZIP.

    ``account_handle`` is the archive owner's handle, supplied by the caller.
    It is never hard-coded.
    """
    if not isinstance(account_handle, str) or not account_handle.strip():
        raise ValueError(
            "account_handle is required and must be a non-empty string. "
            "Do not hard-code an account."
        )
    account_handle = account_handle.strip()
    path = Path(zip_path)
    if not path.is_file():
        raise FileNotFoundError(f"ZIP not found: {path}")

    try:
        zf = ZipFile(path)
    except BadZipFile:
        return _blocked(
            account_handle,
            "NOT_A_ZIP",
            "Path is not a readable ZIP. No archive schema was assumed.",
        )

    with zf:
        names = [info.filename for info in zf.infolist() if not info.is_dir()]
        classified = {name: classify_archive_path(name) for name in names}

        files_accessed: list[str] = []
        files_ignored = [
            {"path": name, "category": classified[name]}
            for name in names
            if classified[name]
            in {
                "direct_message",
                "tweet",
                "ad",
                "personalization",
                "contact",
                "location",
                "media",
            }
        ]
        manifest_names = [name for name in names if classified[name] == "manifest"]
        follower_names = [name for name in names if classified[name] == "follower"]
        following_names = [name for name in names if classified[name] == "following"]

        export_timestamp = None
        export_timestamp_key = None
        manifest_paths: set[str] = set()
        manifest_error = None

        for name in manifest_names:
            files_accessed.append(name)
            try:
                parsed = _parse_json_container(_read_member(zf, name))
            except (ValueError, json.JSONDecodeError, UnicodeError) as exc:
                manifest_error = f"{name}: {exc.__class__.__name__}"
                continue
            _collect_path_strings(parsed, manifest_paths)
            stamp, key = _export_timestamp_from_manifest(parsed)
            if stamp and export_timestamp is None:
                export_timestamp = stamp
                export_timestamp_key = key

        # Manifest may name a follower file. Only open it if the path is in
        # the ZIP and classifies as follower. Never open a path just because
        # the manifest mentioned it.
        for mentioned in sorted(manifest_paths):
            if mentioned in classified and classified[mentioned] == "follower":
                if mentioned not in follower_names:
                    follower_names.append(mentioned)
            elif mentioned in classified and classified[mentioned] in {
                "direct_message",
                "tweet",
                "ad",
                "personalization",
                "contact",
                "location",
                "media",
            }:
                entry = {"path": mentioned, "category": classified[mentioned]}
                if entry not in files_ignored:
                    files_ignored.append(entry)

        files_ignored.sort(key=lambda item: item["path"])

        accessed_set = set(files_accessed)
        ignored_set = {item["path"] for item in files_ignored}
        files_not_accessed = [
            name
            for name in names
            if name not in accessed_set
            and name not in ignored_set
            and name not in follower_names
            and classified[name] != "follower"
        ]
        # following is follower-related but is not the follower list. It is
        # not opened in this pass: relationship booleans are taken only from
        # fields present on follower records. Record the path as not accessed.
        for name in following_names:
            if name not in files_not_accessed:
                files_not_accessed.append(name)

        provenance = {
            "source": SOURCE,
            "account_handle": account_handle,
            "account_handle_origin": "caller_parameter",
            "export_timestamp": export_timestamp or NOT_AVAILABLE,
            "export_timestamp_key": export_timestamp_key,
            "schema": "UNKNOWN",
            "schema_note": (
                "X archive field names are not verified. "
                "Records below use only keys present on the supplied follower file "
                "that match the normalized contract. "
                "No X field names were assumed."
            ),
            "manifest_parse_note": manifest_error,
        }

        if not follower_names:
            return _blocked(
                account_handle,
                "NO_RECOGNIZABLE_FOLLOWER_FILE",
                (
                    "ZIP has no member whose path is a follower file "
                    "(path token 'follower' or 'followers'). "
                    "Refusing to scan other JSON for follower fields."
                ),
                files_accessed=files_accessed,
                files_ignored=files_ignored,
                files_not_accessed=sorted(files_not_accessed),
                provenance=provenance,
            )

        records: list[dict[str, Any]] = []
        observed: set[str] = set()
        unrecognized: list[str] = []
        for name in follower_names:
            files_accessed.append(name)
            try:
                parsed = _parse_json_container(_read_member(zf, name))
            except (ValueError, json.JSONDecodeError, UnicodeError) as exc:
                return _blocked(
                    account_handle,
                    "UNPARSEABLE_FOLLOWER_FILE",
                    (
                        f"Follower file {name} could not be parsed as JSON "
                        f"({exc.__class__.__name__}). Field names were not guessed."
                    ),
                    files_accessed=files_accessed,
                    files_ignored=files_ignored,
                    files_not_accessed=sorted(
                        n for n in files_not_accessed if n not in files_accessed
                    ),
                    provenance=provenance,
                )
            status, parsed_records, keys, detail = _normalize_follower_payload(
                parsed, archive_export_timestamp=export_timestamp
            )
            observed.update(keys)
            if status == "unrecognized":
                unrecognized.append(f"{name}: {detail}")
                continue
            records.extend(parsed_records)

        files_not_accessed = sorted(
            name for name in files_not_accessed if name not in set(files_accessed)
        )

        if unrecognized and not records:
            return _blocked(
                account_handle,
                "UNRECOGNIZED_FOLLOWER_SCHEMA",
                (
                    "A follower-named file was found, but its keys do not include "
                    "x_user_id or handle. Refusing to guess field names. "
                    + "; ".join(unrecognized)
                ),
                files_accessed=files_accessed,
                files_ignored=files_ignored,
                files_not_accessed=files_not_accessed,
                observed_follower_keys=sorted(observed),
                provenance=provenance,
            )

        return {
            "status": "OK",
            "block_reason": None,
            "message": (
                "Follower file parsed from keys present on the supplied objects. "
                "Schema of a real X archive remains UNKNOWN."
            ),
            "account_handle": account_handle,
            "provenance": provenance,
            "files_accessed": files_accessed,
            "files_ignored": files_ignored,
            "files_not_accessed": files_not_accessed,
            "observed_follower_keys": sorted(observed),
            "unrecognized_follower_files": unrecognized,
            "records": records,
            "record_count": len(records),
        }


def _identity(record: Mapping[str, Any]) -> str | None:
    user_id = record.get("x_user_id")
    if isinstance(user_id, str) and user_id.isdigit():
        return "id:" + user_id
    handle = record.get("handle")
    if isinstance(handle, str) and handle.strip() and handle != NOT_AVAILABLE:
        return "handle:" + handle.strip().lstrip("@").lower()
    return None


def _stamp(record: Mapping[str, Any]) -> str:
    value = record.get("source_timestamp")
    if isinstance(value, str) and value.strip() and value != NOT_AVAILABLE:
        return value
    return NOT_AVAILABLE


def _compare_view(record: Mapping[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {}
    missing = record.get("missing") if isinstance(record.get("missing"), dict) else {}
    for key in DELTA_COMPARE_FIELDS:
        if key in record:
            view[key] = deepcopy(record[key])
        else:
            view[key] = missing.get(key, NOT_AVAILABLE)
    return view


def diff_follower_snapshots(
    baseline_records: list[Mapping[str, Any]],
    current_records: list[Mapping[str, Any]],
    *,
    baseline_export_timestamp: str | None = None,
    current_export_timestamp: str | None = None,
) -> dict[str, Any]:
    """Delta between two already-normalized follower snapshots.

    ``disappeared`` means the identity is absent from the later snapshot.
    That is not a Follower Finder removal. Cause is UNKNOWN (unfollow, archive
    omission, or something else). Timestamps already on the records are copied
    forward and never replaced with the current clock.

    Identities whose comparable fields differ are ``changed``, not ``unchanged``.
    """
    def index(records: list[Mapping[str, Any]]) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
        out: dict[str, Mapping[str, Any]] = {}
        duplicates: list[str] = []
        for record in records:
            identity = _identity(record)
            if identity is None:
                duplicates.append("unidentified")
                continue
            if identity in out:
                duplicates.append(identity)
                continue
            out[identity] = record
        return out, duplicates

    before, before_dups = index(list(baseline_records))
    after, after_dups = index(list(current_records))

    new_identities = []
    disappeared = []
    unchanged = []
    changed = []

    for identity in sorted(before):
        baseline = before[identity]
        if identity not in after:
            disappeared.append(
                {
                    "identity": identity,
                    "state": "disappeared",
                    "not_a_bot_terminator_removal": True,
                    "meaning": "ABSENT_FROM_LATER_ARCHIVE",
                    "cause": "UNKNOWN",
                    "baseline_source_timestamp": _stamp(baseline),
                    "current_source_timestamp": NOT_AVAILABLE,
                    "record": deepcopy(dict(baseline)),
                }
            )
            continue
        current = after[identity]
        if _compare_view(baseline) == _compare_view(current):
            unchanged.append(
                {
                    "identity": identity,
                    "state": "unchanged",
                    "baseline_source_timestamp": _stamp(baseline),
                    "current_source_timestamp": _stamp(current),
                    "record": deepcopy(dict(baseline)),
                    "current_record": deepcopy(dict(current)),
                }
            )
        else:
            before_view = _compare_view(baseline)
            after_view = _compare_view(current)
            fields = sorted(
                key for key in DELTA_COMPARE_FIELDS if before_view.get(key) != after_view.get(key)
            )
            changed.append(
                {
                    "identity": identity,
                    "state": "changed",
                    "baseline_source_timestamp": _stamp(baseline),
                    "current_source_timestamp": _stamp(current),
                    "changed_fields": fields,
                    "record": deepcopy(dict(baseline)),
                    "current_record": deepcopy(dict(current)),
                }
            )

    for identity in sorted(after):
        if identity in before:
            continue
        current = after[identity]
        new_identities.append(
            {
                "identity": identity,
                "state": "new_identity",
                "meaning": "PRESENT_IN_LATER_ARCHIVE_ABSENT_FROM_BASELINE",
                "cause": "UNKNOWN",
                "baseline_source_timestamp": NOT_AVAILABLE,
                "current_source_timestamp": _stamp(current),
                "record": deepcopy(dict(current)),
            }
        )

    return {
        "baseline_export_timestamp": baseline_export_timestamp or NOT_AVAILABLE,
        "current_export_timestamp": current_export_timestamp or NOT_AVAILABLE,
        "new_identities": new_identities,
        "disappeared": disappeared,
        "unchanged": unchanged,
        "changed": changed,
        "not_a_bot_terminator_removal": True,
        "disappeared_meaning": "ABSENT_FROM_LATER_ARCHIVE",
        "cause": "UNKNOWN",
        "duplicate_identities": {
            "baseline": before_dups,
            "current": after_dups,
        },
    }


def diff_archive_loads(baseline: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """Delta two ``load_x_archive_followers`` results. Does not classify."""
    if baseline.get("status") != "OK" or current.get("status") != "OK":
        return {
            "status": "BLOCKED",
            "block_reason": "DELTA_REQUIRES_TWO_OK_LOADS",
            "message": "Refusing to diff a blocked archive load.",
            "new_identities": [],
            "disappeared": [],
            "unchanged": [],
            "changed": [],
            "not_a_bot_terminator_removal": True,
        }
    base_prov = baseline.get("provenance") if isinstance(baseline.get("provenance"), dict) else {}
    cur_prov = current.get("provenance") if isinstance(current.get("provenance"), dict) else {}
    delta = diff_follower_snapshots(
        list(baseline.get("records") or []),
        list(current.get("records") or []),
        baseline_export_timestamp=_string_or_none(base_prov.get("export_timestamp")),
        current_export_timestamp=_string_or_none(cur_prov.get("export_timestamp")),
    )
    delta["status"] = "OK"
    return delta


def hand_records_to_existing_pipeline(
    records: list[Mapping[str, Any]],
    pipeline: Callable[[list[Mapping[str, Any]]], Any],
) -> Any:
    """Hand already-normalized records to a caller-supplied pipeline.

    This function does not import or call the classifier. STOP POINT A does
    not run classification.
    """
    if pipeline is None:
        raise RuntimeError(
            "No pipeline was injected. Classification is out of scope for this adapter."
        )
    return pipeline(list(records))
