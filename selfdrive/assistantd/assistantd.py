#!/usr/bin/env python3
import time
import os
import json
import traceback

import cereal.messaging as messaging
from cereal.services import SERVICE_LIST, QueueSize
from openpilot.common.swaglog import cloudlog
from openpilot.common.realtime import set_core_affinity, set_realtime_priority

# ------------------------------------------------------------------------------
# Comma 4 Assistant Daemon (assistantd)
# This background process reads all relevant telemetry streams from openpilot
# and acts as the bridge to your homelab AI swarm.
# ------------------------------------------------------------------------------

def get_safe_services():
  """
  Returns a list of all non-massive cereal services to subscribe to safely.
  Filters out BIG queues like raw video frames or massive raw NN output.
  """
  allowed_services = []
  for name, service in SERVICE_LIST.items():
    if service.queue_size != QueueSize.BIG:
      allowed_services.append(name)
  return allowed_services

def extract_message_data(msg, service_name):
  """
  Helper function to extract meaning full JSON-serializable data from capnp structs.
  Will serialize the message dictionary structure.
  """
  try:
    # Convert capnp to dict
    data_dict = msg.to_dict()
    # Remove some bulky binary/byte fields if they exist
    if 'logMonoTime' in data_dict:
      del data_dict['logMonoTime']

    return data_dict
  except Exception:
    return {"error": "Failed to parse"}

def assistantd_thread():
  cloudlog.info("assistantd: starting")

  # 1. Setup cereal subscribers
  # Read absolutely everything possible that isn't heavy video chunks
  services_to_read = get_safe_services()
  cloudlog.info(f"assistantd: Subscribing to {len(services_to_read)} cereal services...")

  # Initialize the SubMaster which manages pulling all these sockets efficiently
  sm = messaging.SubMaster(services_to_read)

  # (Optional) If we want to publish data outward (e.g. asking the UI to draw something, or sending CAN)
  # pm = messaging.PubMaster(['uiDebug'])

  # Setup our context state to stream to the homelab
  live_context = {}

  # Run loop at 10Hz (0.1s interval)
  # This provides a responsive assistant experience, we will bundle changes every tick
  while True:
    try:
      # Pull latest messages from all subscribed sockets
      sm.update(100) # Wait up to 100ms for a message

      # Check which topics updated and store their data
      for service_name in services_to_read:
        if sm.updated.get(service_name, False):
          msg = sm[service_name]
          live_context[service_name] = extract_message_data(msg, service_name)

      # ------------------------------------------------------------------------
      # TODO: HOMELAB INTEGRATION
      # Here is where you integrate the network call to your FastAPI / WebSocket
      # on your homelab.
      # You can send `live_context` (a dict containing the current state of the car).
      # ------------------------------------------------------------------------

      # Example of context that is rich and useful for the swarm:
      # - live_context.get('carState') -> speed, steering angle, pedals, blinkers, doors, seatbelts
      # - live_context.get('gpsLocationExternal') -> latitude, longitude, altitude
      # - live_context.get('managerState') -> list of all running processes and if they crashed
      # - live_context.get('controlsState') -> ADAS engagement status, current engaged speed

      # (Example debug print, only prints if running manually)
      if os.getenv("ASSISTANT_DEBUG"):
        if 'carState' in live_context:
          print(f"Speed: {live_context['carState'].get('vEgo', 0):.2f} m/s")

    except Exception:
      cloudlog.error("assistantd: crashed in main loop")
      cloudlog.error(traceback.format_exc())

    # Sleep slightly to maintain loop frequency
    time.sleep(0.05)


def main():
  # Set up process priorities for stability on the device
  set_core_affinity([0, 1, 2, 3])
  set_realtime_priority(1)

  try:
    assistantd_thread()
  except Exception:
    cloudlog.error("assistantd: unhandled exception")
    cloudlog.error(traceback.format_exc())


if __name__ == "__main__":
  main()
