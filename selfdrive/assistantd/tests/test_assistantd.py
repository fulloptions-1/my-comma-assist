from selfdrive.assistantd.assistantd import AssistantConfig, DisabledProvider, RunDecision, RunKernel, extract_message_data, select_services


class FakeMsg:
  def __init__(self, data):
    self.data = data

  def to_dict(self):
    return self.data


class FailingMsg:
  def to_dict(self):
    raise ValueError("bad capnp")


class FakeSubMaster:
  def __init__(self):
    self.updated = {"carState": True}
    self.messages = {"carState": FakeMsg({"logMonoTime": 10, "vEgo": 7.5, "raw": b"abc"})}
    self.update_calls = []

  def update(self, timeout):
    self.update_calls.append(timeout)

  def __getitem__(self, key):
    return self.messages[key]


class CapturingProvider:
  def __init__(self, failures=0):
    self.failures = failures
    self.payloads = []

  def send(self, payload):
    self.payloads.append(payload)
    if len(self.payloads) <= self.failures:
      raise RuntimeError("temporary")


def test_extract_message_data_removes_log_time_and_scrubs_bytes():
  data = extract_message_data(FakeMsg({"logMonoTime": 123, "nested": {"blob": b"1234"}}), "carState")
  assert data == {"_service": "carState", "nested": {"blob": "<bytes:4>"}}


def test_extract_message_data_reports_parse_errors_without_crashing():
  assert extract_message_data(FailingMsg(), "carState") == {"_service": "carState", "error": "Failed to parse: ValueError"}


def test_kernel_dispatches_direct_provider_invocation_after_state_update():
  config = AssistantConfig(enabled=True, endpoint="http://example", services=("carState",), max_retries=0)
  provider = CapturingProvider()
  kernel = RunKernel(config, provider)
  decision = kernel.step(FakeSubMaster(), ["carState"])
  assert decision == RunDecision.CONTINUE
  assert kernel.state.sent_count == 1
  assert provider.payloads[0]["context"]["carState"]["vEgo"] == 7.5


def test_kernel_retries_provider_failures_and_recovers():
  config = AssistantConfig(enabled=True, endpoint="http://example", max_retries=1, retry_backoff_s=0)
  provider = CapturingProvider(failures=1)
  kernel = RunKernel(config, provider, sleeper=lambda _: None)
  kernel.state.live_context["carState"] = {"vEgo": 1.0}
  assert kernel.dispatch() == RunDecision.CONTINUE
  assert kernel.state.failure_count == 1
  assert kernel.state.sent_count == 1


def test_kernel_enforces_payload_budget():
  config = AssistantConfig(enabled=True, endpoint="http://example", max_payload_bytes=10)
  kernel = RunKernel(config, CapturingProvider())
  kernel.state.live_context["carState"] = {"large": "x" * 100}
  assert kernel.dispatch() == RunDecision.BUDGET_EXHAUSTED
  assert kernel.state.dropped_count == 1


def test_disabled_provider_does_not_send_network_data():
  config = AssistantConfig(enabled=False)
  kernel = RunKernel(config, DisabledProvider())
  kernel.state.live_context["carState"] = {"vEgo": 1.0}
  assert kernel.dispatch() == RunDecision.DISABLED


def test_select_services_filters_unknown_requested_services():
  config = AssistantConfig(services=("carState", "notAService"))
  assert select_services(config, safe_services=["carState", "controlsState"]) == ["carState"]
