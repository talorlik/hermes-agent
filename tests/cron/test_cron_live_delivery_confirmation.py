"""Live-adapter delivery confirmation for cron (#77763).

A ``no_agent`` job fired, the scheduler logged
``delivered to telegram:<chat> via live adapter``, and the user received
nothing — no message row, no delivery obligation. The log line was not
evidence of a send:

* the silence-narration filter returns ``{"success": True, "delivered": False}``
  (a successful *drop*), and the normalization block read only ``success``;
* an empty payload skipped the send entirely and still fell into the
  "delivered" branch;
* the log line named the chat but not the lane, so a wrong-thread delivery and
  a phantom one look identical after the fact.

These tests pin the confirmation contract: positive evidence, honest logging,
and fail-closed on nothing-to-send.
"""

import asyncio
import logging
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest

from cron import scheduler as sched
from cron import scheduler_delivery as sched_delivery
from cron.scheduler import _deliver_result
from cron.scheduler_delivery import _confirm_adapter_delivery
from gateway.config import Platform, PlatformConfig


# ---------------------------------------------------------------------------
# _confirm_adapter_delivery: the contract in isolation
# ---------------------------------------------------------------------------

class _SendResult:
    """Minimal stand-in for an adapter SendResult."""

    def __init__(self, success=True, message_id=None, raw_response=None, **extra):
        self.success = success
        self.message_id = message_id
        self.raw_response = raw_response
        for key, value in extra.items():
            setattr(self, key, value)


class TestConfirmAdapterDelivery:
    def test_none_is_not_delivered(self):
        assert _confirm_adapter_delivery(None, "j1") is False

    def test_missing_success_is_not_delivered(self):
        assert _confirm_adapter_delivery(object(), "j1") is False
        assert _confirm_adapter_delivery({"message_id": 7}, "j1") is False

    def test_explicit_failure_is_not_delivered(self):
        assert _confirm_adapter_delivery(_SendResult(success=False), "j1") is False
        assert _confirm_adapter_delivery({"success": False}, "j1") is False

    def test_filtered_dict_is_not_delivered(self):
        """The exact silence-filter shape: a successful DROP is not a delivery."""
        filtered = {"success": True, "filtered": "silence_narration", "delivered": False}
        assert _confirm_adapter_delivery(filtered, "j1") is False

    def test_delivered_false_on_an_object_is_not_delivered(self):
        result = _SendResult(success=True, message_id=42, delivered=False)
        assert _confirm_adapter_delivery(result, "j1") is False

    def test_positive_evidence_is_delivered_without_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            assert _confirm_adapter_delivery(_SendResult(message_id=1234), "j1") is True
        assert "UNVERIFIED" not in caplog.text

    def test_raw_response_alone_counts_as_evidence(self, caplog):
        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            result = _SendResult(raw_response={"ok": True})
            assert _confirm_adapter_delivery(result, "j1") is True
        assert "UNVERIFIED" not in caplog.text

    def test_evidence_free_success_is_accepted_but_warned(self, caplog):
        """Not proof of failure either — accept it, but say so in the log."""
        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            assert _confirm_adapter_delivery(_SendResult(), "92e639af907f") is True
        assert "UNVERIFIED" in caplog.text
        assert "92e639af907f" in caplog.text

    def test_evidence_free_success_dict_is_accepted_but_warned(self, caplog):
        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            assert _confirm_adapter_delivery({"success": True}, "j1") is True
        assert "UNVERIFIED" in caplog.text


# ---------------------------------------------------------------------------
# _deliver_result: the live lane end to end
# ---------------------------------------------------------------------------

CHAT_ID = "-1001234567890"


def _job(thread_id=None):
    origin = {"platform": "telegram", "chat_id": CHAT_ID}
    if thread_id is not None:
        origin["thread_id"] = thread_id
    return {
        "id": "92e639af907f",
        "name": "Ghost Delivery",
        "deliver": "origin",
        "origin": origin,
    }


def _gateway_config(relay=False):
    config = MagicMock()
    platforms = {Platform.TELEGRAM: PlatformConfig(enabled=True)}
    if relay:
        platforms[Platform.RELAY] = PlatformConfig(enabled=True)
    config.platforms = platforms
    config.get_home_channel = lambda p: None
    return config


def _adapters(relay=False):
    adapter = MagicMock()
    if relay:
        adapter.fronts_platform = lambda p: p == Platform.TELEGRAM
        return {Platform.RELAY: adapter}
    return {Platform.TELEGRAM: adapter}


RECORDED_VERIFICATION = []

# Transport identity/authority capture (#115656): the ``DeliveryRouter`` the live lane builds
# (``config``/``adapters`` it was handed) and the ``(transport, pconfig)`` the lane itself resolved
# and authorized per target, so a test can assert the router receives the ORIGINAL gateway config
# and the EXACT authorized transport object rather than a re-derived or re-configured one.
ROUTER_CONSTRUCTIONS = []
RESOLVED_TRANSPORTS = []


def _record_verification(job, unverified_targets):
    RECORDED_VERIFICATION.append((job["id"], list(unverified_targets)))


def _run(job, content, send_result, relay=False, standalone_result=None, cron_cfg=None, *,
         adapters=None, gateway_config=None):
    """Drive ``_deliver_result`` over the live lane with a stubbed router.

    Returns ``(error, router_calls, standalone_calls)``. ``cron_cfg`` extends
    the ``cron:`` section handed to the scheduler (default: unwrapped output).
    ``adapters``/``gateway_config`` override the default ``relay``-shaped
    fixtures (satellite grant / disabled-block scenarios).
    """
    loop = MagicMock()
    loop.is_running.return_value = True

    def fake_run_coro(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as e:
            future.set_exception(e)
        return future

    router_calls = []
    standalone_calls = []
    RECORDED_VERIFICATION.clear()
    ROUTER_CONSTRUCTIONS.clear()
    RESOLVED_TRANSPORTS.clear()

    router = MagicMock()

    # Mirrors the production ``DeliveryRouter._deliver_to_platform(target, content, metadata,
    # transport=None)`` signature (gateway/delivery.py): ``transport`` is the already-authorized
    # transport the cron lane hands past resolution; ``None`` means "resolve as usual".
    async def _deliver_to_platform(target, text, metadata, transport=None):
        router_calls.append({
            "target": target, "text": text, "metadata": metadata, "transport": transport,
        })
        return send_result

    router._deliver_to_platform = _deliver_to_platform

    def _build_router(config, adapters_arg):
        ROUTER_CONSTRUCTIONS.append({"config": config, "adapters": adapters_arg})
        return router

    real_resolve = sched_delivery._resolve_target_transport

    def _recording_resolve(*args, **kwargs):
        resolved, err = real_resolve(*args, **kwargs)
        RESOLVED_TRANSPORTS.append({"resolved": resolved, "error": err})
        return resolved, err

    async def _fake_send_to_platform(platform, pconfig, chat_id, text, **kwargs):
        standalone_calls.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        return standalone_result if standalone_result is not None else {}

    if gateway_config is None:
        gateway_config = _gateway_config(relay)
    if adapters is None:
        adapters = _adapters(relay)

    with patch("gateway.config.load_gateway_config", return_value=gateway_config), \
         patch("cron.scheduler.load_config",
               return_value={"cron": {"wrap_response": False, **(cron_cfg or {})}}), \
         patch("cron.scheduler_delivery._record_delivery_verification", side_effect=_record_verification), \
         patch("cron.scheduler_delivery._resolve_target_transport", side_effect=_recording_resolve), \
         patch("gateway.delivery.DeliveryRouter", side_effect=_build_router), \
         patch("tools.send_message_tool._send_to_platform", _fake_send_to_platform), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
        error = _deliver_result(job, content, adapters=adapters, loop=loop)
    return error, router_calls, standalone_calls


class TestFilteredResultIsNotDelivered:
    FILTERED = {"success": True, "filtered": "silence_narration", "delivered": False}

    def test_filtered_dict_does_not_log_a_live_delivery(self, caplog):
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            _, router_calls, standalone_calls = _run(_job(), "...", self.FILTERED)

        assert len(router_calls) == 1                     # the live send was attempted
        assert "via live adapter" not in caplog.text      # but never claimed as delivered
        assert len(standalone_calls) == 1                 # fell back instead of lying

    def test_filtered_dict_fails_closed_on_the_relay_lane(self):
        """Relay owns the destination, so there is no fallback — report it."""
        error, _, standalone_calls = _run(_job(), "...", self.FILTERED, relay=True)

        assert error is not None
        assert "unconfirmed result" in error
        assert "silence_narration" in error  # names the filter, not "unknown"
        assert standalone_calls == []

    def test_confirmed_send_result_still_delivers(self, caplog):
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            error, router_calls, standalone_calls = _run(
                _job(), "Nightly report.", _SendResult(message_id=1234),
            )

        assert error is None
        assert len(router_calls) == 1
        assert standalone_calls == []
        assert "via live adapter" in caplog.text


class TestEmptyPayloadFailsClosed:
    def test_empty_payload_never_reaches_the_adapter(self, caplog):
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            _, router_calls, _ = _run(_job(), "   ", _SendResult(message_id=1))

        assert router_calls == []                     # nothing was sent
        assert "via live adapter" not in caplog.text  # and nothing was claimed
        assert "empty text and no media" in caplog.text

    def test_empty_payload_never_reaches_the_standalone_sender(self, caplog):
        """The native fallback must not re-open the hole the live lane closed.

        Telegram's adapter returns ``SendResult(success=True)`` for empty
        content without an API call, so an unguarded fallback would log a
        standalone "delivered" for the same phantom payload (#77763).
        """
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            error, router_calls, standalone_calls = _run(
                _job(), "   ", _SendResult(message_id=1),
            )

        assert router_calls == []
        assert standalone_calls == []  # _send_to_platform never called
        assert error is not None
        assert "standalone send skipped (empty text and no media)" in error
        assert "delivered to" not in caplog.text

    def test_empty_payload_is_reported_on_the_relay_lane(self):
        error, router_calls, _ = _run(_job(), "", _SendResult(message_id=1), relay=True)

        assert router_calls == []
        assert error is not None
        assert "live adapter send skipped (empty text and no media)" in error


class TestDeliveredLogNamesTheLane:
    def test_log_includes_thread_and_message_id(self, caplog):
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            error, _, _ = _run(
                _job(thread_id="99"), "Nightly report.", _SendResult(message_id=1234),
            )

        assert error is None
        assert "via live adapter thread=99 message_id=1234" in caplog.text

    def test_log_uses_a_dash_when_the_lane_is_unknown(self, caplog):
        """No thread and an evidence-free result must still be attributable."""
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            error, _, _ = _run(_job(), "Nightly report.", _SendResult())

        assert error is None
        assert "via live adapter thread=- message_id=-" in caplog.text
        assert "UNVERIFIED" in caplog.text


class TestLiveDeliveryIsAFinalNotification:
    """Cron output is a final user-visible delivery, not a progress send.

    Telegram's adapter defaults to ``_notifications_mode = "important"`` and
    sends with ``disable_notification=True`` unless ``metadata["notify"]`` is
    set — so a cron brief without the marker lands silently, which users
    report as "never delivered" (#77763 thread, #58258 typing bubble). The
    marker must ride both the text route and the media route, in every
    Telegram routing mode.
    """

    def test_text_route_metadata_carries_notify(self):
        _, router_calls, _ = _run(_job(), "Nightly report.", _SendResult(message_id=1))
        assert len(router_calls) == 1
        metadata = router_calls[0]["metadata"]
        assert metadata["job_id"] == "92e639af907f"
        assert metadata["notify"] is True

    def test_forum_topic_route_keeps_thread_and_notify(self):
        _, router_calls, _ = _run(
            _job(thread_id="99"), "Nightly report.", _SendResult(message_id=1),
        )
        metadata = router_calls[0]["metadata"]
        assert metadata["thread_id"] == "99"
        assert metadata["notify"] is True

    def test_media_route_metadata_carries_notify(self, tmp_path):
        media = tmp_path / "report.png"
        media.write_bytes(b"\x89PNG\r\n\x1a\n")
        sent = []

        def fake_send_media(adapter, chat_id, media_files, metadata, loop, job, platform=None):
            sent.append({"media": list(media_files), "metadata": metadata})
            return []

        with patch("cron.scheduler_delivery._send_media_via_adapter", side_effect=fake_send_media), \
             patch("gateway.platforms.base.BasePlatformAdapter.filter_media_delivery_paths",
                   side_effect=lambda files: files):
            error, router_calls, _ = _run(
                _job(), f"Nightly report.\nMEDIA:{media}", _SendResult(message_id=1),
            )

        assert error is None
        assert len(router_calls) == 1
        assert len(sent) == 1
        assert sent[0]["metadata"]["notify"] is True


class TestNotifyIsConfigurable:
    """``cron.delivery.notify`` (config.yaml) gates the notify marker.

    The current behaviour (push notification) stays the default; only an
    explicit ``false`` restores silent deliveries. The knob rides both the
    text route and the media route so the two never disagree.
    """

    def test_default_is_notify(self):
        _, router_calls, _ = _run(_job(), "Nightly report.", _SendResult(message_id=1))
        assert router_calls[0]["metadata"]["notify"] is True

    def test_explicit_false_disables_notify_on_text_route(self):
        _, router_calls, _ = _run(
            _job(thread_id="99"), "Nightly report.", _SendResult(message_id=1),
            cron_cfg={"delivery": {"notify": False}},
        )
        metadata = router_calls[0]["metadata"]
        assert metadata["notify"] is False
        assert metadata["thread_id"] == "99"  # routing untouched

    def test_explicit_false_disables_notify_on_media_route(self, tmp_path):
        media = tmp_path / "report.png"
        media.write_bytes(b"\x89PNG\r\n\x1a\n")
        sent = []

        def fake_send_media(adapter, chat_id, media_files, metadata, loop, job, platform=None):
            sent.append(metadata)
            return []

        with patch("cron.scheduler_delivery._send_media_via_adapter", side_effect=fake_send_media), \
             patch("gateway.platforms.base.BasePlatformAdapter.filter_media_delivery_paths",
                   side_effect=lambda files: files):
            _run(
                _job(), f"Nightly report.\nMEDIA:{media}", _SendResult(message_id=1),
                cron_cfg={"delivery": {"notify": False}},
            )
        assert sent[0]["notify"] is False

    @pytest.mark.parametrize("cron_cfg", [
        {"delivery": None},            # `delivery:` with no body parses to null
        {"delivery": "yes"},           # malformed scalar
        {"delivery": {"notify": None}},  # `notify:` with no value
    ])
    def test_malformed_section_keeps_the_default(self, cron_cfg):
        _, router_calls, _ = _run(_job(), "Nightly report.", _SendResult(message_id=1), cron_cfg=cron_cfg)
        assert router_calls[0]["metadata"]["notify"] is True

    def test_default_config_ships_notify_true(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["cron"]["delivery"]["notify"] is True


class TestUnverifiedDeliveryIsRecordedOnTheJob:
    """An evidence-free ack is accepted, but the state must reach the job
    record (and from there ``hermes cron list`` / ``cron doctor``), not only a
    WARNING log line."""

    def test_evidence_free_ack_records_the_target(self):
        error, _, _ = _run(_job(), "Nightly report.", _SendResult())
        assert error is None
        assert RECORDED_VERIFICATION == [("92e639af907f", [f"telegram:{CHAT_ID}"])]

    def test_positive_evidence_clears_the_marker(self):
        error, _, _ = _run(_job(), "Nightly report.", _SendResult(message_id=1234))
        assert error is None
        assert RECORDED_VERIFICATION == [("92e639af907f", [])]

    def test_recorder_skips_the_write_when_nothing_changed(self):
        with patch("cron.jobs.update_job") as update_job:
            job = {
                "id": "j1",
                "execution_id": "exec-1",
                "_delivery_projection_revision": 7,
                "last_delivery_unverified": None,
            }
            sched_delivery._record_delivery_verification(job, [])
            update_job.assert_not_called()
            sched_delivery._record_delivery_verification(job, ["slack:C1"])
            update_job.assert_called_once_with(
                "j1",
                {"last_delivery_unverified": ["slack:C1"]},
                expected_execution_id="exec-1",
                expected_projection_revision=7,
            )

    def test_recorder_clears_a_stale_marker(self):
        with patch("cron.jobs.update_job") as update_job:
            sched_delivery._record_delivery_verification(
                {
                    "id": "j1",
                    "execution_id": "exec-1",
                    "_delivery_projection_revision": 7,
                    "last_delivery_unverified": ["slack:C1"],
                },
                [],
            )
            update_job.assert_called_once_with(
                "j1",
                {"last_delivery_unverified": None},
                expected_execution_id="exec-1",
                expected_projection_revision=7,
            )

    def test_tool_listing_exposes_the_field(self):
        from tools.cronjob_tools import _format_job

        assert _format_job({"id": "j1", "name": "n", "prompt": "p",
                            "last_delivery_unverified": ["slack:C1"]})["last_delivery_unverified"] == ["slack:C1"]


class _Route:
    """Target-exact primary route as ``SharedRouteAdapters.get`` consumes it (``platform``,
    ``chat_id``/``thread_id`` discriminators, ``guild_id`` echoed, ``matches(...)``)."""

    def __init__(self, platform, chat_id, thread_id=None):
        self.platform = platform
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.guild_id = None

    def matches(self, platform, *, guild_id=None, chat_id=None, thread_id=None):
        return (
            str(platform).lower() == str(self.platform).lower()
            and chat_id == self.chat_id
            and (self.thread_id is None or thread_id == self.thread_id)
        )


def _satellite_adapters(granted=True):
    """Credentialless satellite view over the PRIMARY's telegram adapter (#101113)."""
    from cron.scheduler_preflight import SharedRouteAdapters

    primary = {Platform.TELEGRAM: MagicMock(name="primary_telegram_adapter")}
    routes = [_Route("telegram", CHAT_ID if granted else "-1009999999999")]
    return SharedRouteAdapters(primary, routes), primary[Platform.TELEGRAM]


def _satellite_config():
    """The satellite's own ``platforms.telegram`` block: present but disabled (no credential)."""
    config = _gateway_config()
    config.platforms[Platform.TELEGRAM] = PlatformConfig(enabled=False)
    return config


class TestLiveLaneTransportIdentity:
    """The cron live lane resolves and authorizes ONE transport per target (#115656), then hands
    that exact object to ``DeliveryRouter._deliver_to_platform(..., transport=...)``.

    The router must receive the ORIGINAL gateway config (no per-target config rewrite) and the
    IDENTICAL transport the lane authorized — never a re-resolution against the plain adapter
    dict (which cannot re-derive the satellite grant) and never a freshly built adapter grant.
    """

    @staticmethod
    def _authorized_transport():
        assert len(RESOLVED_TRANSPORTS) == 1
        resolved = RESOLVED_TRANSPORTS[0]["resolved"]
        assert resolved is not None, RESOLVED_TRANSPORTS[0]["error"]
        transport, pconfig, runtime_adapter, _target_adapters = resolved
        return transport, pconfig, runtime_adapter

    def test_ordinary_native_target_forwards_the_exact_authorized_transport(self):
        from gateway.delivery import DeliveryTransport

        gateway_config = _gateway_config()
        adapters = _adapters()
        error, router_calls, standalone_calls = _run(
            _job(), "Nightly report.", _SendResult(message_id=1234),
            adapters=adapters, gateway_config=gateway_config,
        )

        assert error is None
        assert standalone_calls == []
        assert len(router_calls) == 1
        transport, _pconfig, runtime_adapter = self._authorized_transport()
        assert isinstance(transport, DeliveryTransport)
        assert not transport.is_relay
        assert transport.adapter is adapters[Platform.TELEGRAM]
        assert runtime_adapter is adapters[Platform.TELEGRAM]
        # Exact object identity: the authorized transport, not a re-resolution or a copy.
        assert router_calls[0]["transport"] is transport
        # The router keeps the original config and adapter map: no per-target replacement.
        assert ROUTER_CONSTRUCTIONS == [{"config": gateway_config, "adapters": adapters}]
        assert ROUTER_CONSTRUCTIONS[0]["config"] is gateway_config
        assert ROUTER_CONSTRUCTIONS[0]["adapters"] is adapters

    def test_relay_fronted_target_forwards_the_relay_transport_binding(self):
        gateway_config = _gateway_config(relay=True)
        adapters = _adapters(relay=True)
        error, router_calls, standalone_calls = _run(
            _job(), "Nightly report.", _SendResult(message_id=1234), relay=True,
            adapters=adapters, gateway_config=gateway_config,
        )

        assert error is None
        assert standalone_calls == []
        assert len(router_calls) == 1
        transport, _pconfig, _runtime_adapter = self._authorized_transport()
        assert transport.is_relay
        assert transport.adapter is adapters[Platform.RELAY]
        assert router_calls[0]["transport"] is transport
        assert router_calls[0]["target"].platform == Platform.TELEGRAM
        assert ROUTER_CONSTRUCTIONS[0]["config"] is gateway_config
        assert ROUTER_CONSTRUCTIONS[0]["adapters"] is adapters

    def test_satellite_grant_preserves_binding_without_config_replacement(self):
        """Disabled satellite ``platforms.telegram`` block + exact primary route: the lane's
        authorized transport (primary adapter, enablement-corrected pconfig) reaches the router
        untouched; the router itself sees the ORIGINAL (disabled-block) config."""
        shared, primary_adapter = _satellite_adapters(granted=True)
        gateway_config = _satellite_config()
        error, router_calls, standalone_calls = _run(
            _job(), "Nightly report.", _SendResult(message_id=1234),
            adapters=shared, gateway_config=gateway_config,
        )

        assert error is None
        assert standalone_calls == []
        assert len(router_calls) == 1
        transport, pconfig, runtime_adapter = self._authorized_transport()
        assert not transport.is_relay
        assert transport.adapter is primary_adapter
        assert runtime_adapter is primary_adapter
        assert transport.config is pconfig
        assert pconfig.enabled is True
        assert router_calls[0]["transport"] is transport
        # No production config rewrite: the satellite block stays disabled in the config the
        # router was built with, and that config is the very object the gateway loaded.
        assert ROUTER_CONSTRUCTIONS[0]["config"] is gateway_config
        assert gateway_config.platforms[Platform.TELEGRAM].enabled is False
        assert ROUTER_CONSTRUCTIONS[0]["adapters"] == {Platform.TELEGRAM: primary_adapter}

    def test_disabled_block_without_a_grant_never_sends(self):
        """Satellite with a disabled block and NO matching primary route: fail closed. No live
        send, no standalone send, no arbitrary adapter grant."""
        shared, _primary_adapter = _satellite_adapters(granted=False)
        error, router_calls, standalone_calls = _run(
            _job(), "Nightly report.", _SendResult(message_id=1234),
            adapters=shared, gateway_config=_satellite_config(),
        )

        assert router_calls == []
        assert standalone_calls == []
        assert ROUTER_CONSTRUCTIONS == []
        assert error is not None
        assert "not configured/enabled" in error
        assert len(RESOLVED_TRANSPORTS) == 1
        assert RESOLVED_TRANSPORTS[0]["resolved"] is None

    def test_disabled_native_block_in_a_plain_adapter_map_never_sends(self):
        """A plain adapter dict (no satellite grant) against a disabled block is not authorized:
        presence of an adapter object alone must not grant a transport."""
        adapters = _adapters()
        error, router_calls, standalone_calls = _run(
            _job(), "Nightly report.", _SendResult(message_id=1234),
            adapters=adapters, gateway_config=_satellite_config(),
        )

        assert router_calls == []
        assert standalone_calls == []
        assert ROUTER_CONSTRUCTIONS == []
        assert error is not None
        assert "not configured/enabled" in error

    def test_router_fake_signature_matches_production(self):
        """The stub above must stay honest: production accepts ``transport`` as an optional
        keyword (default ``None``)."""
        import inspect

        from gateway.delivery import DeliveryRouter

        params = inspect.signature(DeliveryRouter._deliver_to_platform).parameters
        assert "transport" in params
        assert params["transport"].default is None


def test_scheduler_module_exposes_the_confirmation_helper():
    """Guard the import surface the delivery block depends on."""
    assert callable(sched_delivery._confirm_adapter_delivery)
