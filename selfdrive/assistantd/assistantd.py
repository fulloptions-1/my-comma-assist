#!/usr/bin/env python3
import json
import os
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib import request


class RunDecision(str, Enum):
  CONTINUE = "continue"
  RETRY = "retry"
  DISABLED = "disabled"
  BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True)
class AssistantConfig:
  endpoint: str = ""
  provider: str = "http"
  enabled: bool = False
  loop_sleep_s: float = 0.05
  update_timeout_ms: int = 100
  max_payload_bytes: int = 262_144
  max_retries: int = 2
  retry_backoff_s: float = 0.05
  services: tuple[str, ...] = ()

  @classmethod
  def from_env(cls) -> "AssistantConfig":
    endpoint = os.getenv("ASSISTANT_ENDPOINT", "").strip()
    services = tuple(s.strip() for s in os.getenv("ASSISTANT_SERVICES", "").split(",") if s.strip())
    return cls(
      endpoint=endpoint,
      provider=os.getenv("ASSISTANT_PROVIDER", "http").strip() or "http",
      enabled=os.getenv("ASSISTANT_ENABLED", "0") == "1" and bool(endpoint),
      loop_sleep_s=float(os.getenv("ASSISTANT_LOOP_SLEEP_S", "0.05")),
      update_timeout_ms=int(os.getenv("ASSISTANT_UPDATE_TIMEOUT_MS", "100")),
      max_payload_bytes=int(os.getenv("ASSISTANT_MAX_PAYLOAD_BYTES", "262144")),
      max_retries=int(os.getenv("ASSISTANT_MAX_RETRIES", "2")),
      retry_backoff_s=float(os.getenv("ASSISTANT_RETRY_BACKOFF_S", "0.05")),
      services=services,
    )


@dataclass
class RunState:
  live_context: dict[str, Any] = field(default_factory=dict)
  sent_count: int = 0
  failure_count: int = 0
  dropped_count: int = 0
  last_error: str = ""


class AssistantProvider:
  def send(self, payload: dict[str, Any]) -> None:
    raise NotImplementedError


class DisabledProvider(AssistantProvider):
  def send(self, payload: dict[str, Any]) -> None:
    return None


class HttpProvider(AssistantProvider):
  def __init__(self, endpoint: str, timeout_s: float = 2.0) -> None:
    self.endpoint = endpoint
    self.timeout_s = timeout_s

  def send(self, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    req = request.Request(self.endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=self.timeout_s) as resp:
      if resp.status >= 400:
        raise RuntimeError(f"assistant provider returned HTTP {resp.status}")


class ProviderFactory:
  @staticmethod
  def build(config: AssistantConfig) -> AssistantProvider:
    if not config.enabled:
      return DisabledProvider()
    if config.provider == "http":
      return HttpProvider(config.endpoint)
    raise ValueError(f"unsupported assistant provider: {config.provider}")


def get_safe_services() -> list[str]:
  from cereal.services import SERVICE_LIST, QueueSize

  return [name for name, service in SERVICE_LIST.items() if service.queue_size != QueueSize.BIG]


def select_services(config: AssistantConfig, safe_services: list[str] | None = None) -> list[str]:
  safe = set(safe_services if safe_services is not None else get_safe_services())
  if not config.services:
    return sorted(safe)
  return [service for service in config.services if service in safe]


def _json_safe(value: Any) -> Any:
  if isinstance(value, bytes):
    return f"<bytes:{len(value)}>"
  if isinstance(value, dict):
    return {str(k): _json_safe(v) for k, v in value.items() if k != "logMonoTime"}
  if isinstance(value, list):
    return [_json_safe(v) for v in value]
  if isinstance(value, tuple):
    return [_json_safe(v) for v in value]
  return value


def extract_message_data(msg: Any, service_name: str) -> dict[str, Any]:
  try:
    data = msg.to_dict()
  except Exception as exc:
    return {"_service": service_name, "error": f"Failed to parse: {exc.__class__.__name__}"}
  safe_data = _json_safe(data)
  if isinstance(safe_data, dict):
    safe_data["_service"] = service_name
    return safe_data
  return {"_service": service_name, "value": safe_data}


class RunKernel:
  def __init__(self, config: AssistantConfig, provider: AssistantProvider, sleeper: Callable[[float], None] = time.sleep) -> None:
    self.config = config
    self.provider = provider
    self.sleeper = sleeper
    self.state = RunState()

  def build_payload(self) -> dict[str, Any]:
    return {
      "schema": "my-comma-assist.context.v1",
      "sentCount": self.state.sent_count,
      "failureCount": self.state.failure_count,
      "context": self.state.live_context,
    }

  def apply_updates(self, sm: Any, services: list[str]) -> bool:
    changed = False
    for service_name in services:
      if sm.updated.get(service_name, False):
        self.state.live_context[service_name] = extract_message_data(sm[service_name], service_name)
        changed = True
    return changed

  def payload_within_budget(self, payload: dict[str, Any]) -> bool:
    return len(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")) <= self.config.max_payload_bytes

  def dispatch(self) -> RunDecision:
    if not self.config.enabled:
      return RunDecision.DISABLED
    payload = self.build_payload()
    if not self.payload_within_budget(payload):
      self.state.dropped_count += 1
      return RunDecision.BUDGET_EXHAUSTED
    for attempt in range(self.config.max_retries + 1):
      try:
        self.provider.send(payload)
        self.state.sent_count += 1
        self.state.last_error = ""
        return RunDecision.CONTINUE
      except Exception as exc:
        self.state.failure_count += 1
        self.state.last_error = f"{exc.__class__.__name__}: {exc}"
        if attempt < self.config.max_retries:
          self.sleeper(self.config.retry_backoff_s)
    return RunDecision.RETRY

  def step(self, sm: Any, services: list[str]) -> RunDecision:
    sm.update(self.config.update_timeout_ms)
    if self.apply_updates(sm, services):
      return self.dispatch()
    return RunDecision.CONTINUE


def assistantd_thread(config: AssistantConfig | None = None, provider: AssistantProvider | None = None) -> None:
  import cereal.messaging as messaging
  from openpilot.common.swaglog import cloudlog

  cloudlog.info("assistantd: starting")
  config = config or AssistantConfig.from_env()
  services_to_read = select_services(config)
  cloudlog.info(f"assistantd: subscribing to {len(services_to_read)} cereal services")
  sm = messaging.SubMaster(services_to_read)
  kernel = RunKernel(config, provider or ProviderFactory.build(config))

  while True:
    try:
      decision = kernel.step(sm, services_to_read)
      if os.getenv("ASSISTANT_DEBUG") and "carState" in kernel.state.live_context:
        print(f"assistantd: decision={decision} carState={kernel.state.live_context['carState']}")
    except Exception:
      cloudlog.error("assistantd: crashed in main loop")
      cloudlog.error(traceback.format_exc())
    time.sleep(config.loop_sleep_s)


def main() -> None:
  from openpilot.common.realtime import set_core_affinity, set_realtime_priority

  set_core_affinity([0, 1, 2, 3])
  set_realtime_priority(1)
  assistantd_thread()


if __name__ == "__main__":
  main()
