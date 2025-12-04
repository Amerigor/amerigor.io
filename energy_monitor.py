"""
Energy monitor utilities for Raspberry Pi.

This module polls data from Shelly devices and multiple inverter manufacturers.
It is intentionally lightweight so it can run on a Raspberry Pi without extra
services. Each device is wrapped in a small client class so that new devices can
be added without changing the main polling loop.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

import requests


@dataclass
class ShellyTelemetry:
    """Normalized snapshot from a Shelly device."""

    power_w: Optional[float]
    temperature_c: Optional[float]
    wifi_rssi: Optional[int]
    update_time: datetime


@dataclass
class InverterTelemetry:
    """Normalized snapshot from a solar inverter."""

    manufacturer: str
    model: str
    ac_power_w: Optional[float]
    dc_voltage_v: Optional[float]
    temperature_c: Optional[float]
    update_time: datetime


class ShellyClient:
    """HTTP client for a Shelly device."""

    def __init__(self, host: str, timeout: float = 5.0) -> None:
        self.base_url = f"http://{host}"
        self.timeout = timeout

    def read_status(self) -> ShellyTelemetry:
        response = requests.get(f"{self.base_url}/status", timeout=self.timeout)
        response.raise_for_status()
        raw = response.json()
        power = None
        temp = None

        if isinstance(raw.get("meters"), list) and raw["meters"]:
            power = _safe_float(raw["meters"][0].get("power"))

        temperature_source = raw.get("temperature")
        if temperature_source is None:
            temperature_source = raw.get("tmp", {}).get("tC")
        temp = _safe_float(temperature_source)

        wifi_rssi = None
        wifi = raw.get("wifi_sta")
        if isinstance(wifi, dict):
            wifi_rssi = wifi.get("rssi")

        return ShellyTelemetry(
            power_w=power,
            temperature_c=temp,
            wifi_rssi=wifi_rssi,
            update_time=_utcnow(),
        )


class InverterClient:
    """Base interface for all inverter clients."""

    manufacturer: str

    def read_status(self) -> InverterTelemetry:
        raise NotImplementedError


class FroniusClient(InverterClient):
    """Client for Fronius inverters using the local Solar API."""

    manufacturer = "Fronius"

    def __init__(self, host: str, device_id: int = 1, timeout: float = 5.0) -> None:
        self.host = host
        self.device_id = device_id
        self.timeout = timeout

    def read_status(self) -> InverterTelemetry:
        url = (
            f"http://{self.host}/solar_api/v1/"
            f"GetInverterRealtimeData.cgi?Scope=Device&DeviceId={self.device_id}"
        )
        response = requests.get(url, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json().get("Body", {}).get("Data", {})

        ac_power = _safe_float(_lookup(payload, ["PAC", "Value"]))
        dc_voltage = _safe_float(_lookup(payload, ["UDC", "Value"]))
        temperature = _safe_float(_lookup(payload, ["Temperature", "Value"]))

        return InverterTelemetry(
            manufacturer=self.manufacturer,
            model=str(_lookup(payload, ["Details", "Model"])) or "Fronius",  # type: ignore[arg-type]
            ac_power_w=ac_power,
            dc_voltage_v=dc_voltage,
            temperature_c=temperature,
            update_time=_utcnow(),
        )


class SolarEdgeClient(InverterClient):
    """Client for SolarEdge inverters via the local gateway."""

    manufacturer = "SolarEdge"

    def __init__(self, host: str, api_key: Optional[str] = None, timeout: float = 5.0) -> None:
        self.host = host
        self.api_key = api_key
        self.timeout = timeout

    def read_status(self) -> InverterTelemetry:
        url = f"http://{self.host}/api/v1/currentData"
        headers = {"X-API-KEY": self.api_key} if self.api_key else None
        response = requests.get(url, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        data = response.json().get("inverters")
        inverter = data[0] if isinstance(data, list) and data else {}

        ac_power = _safe_float(inverter.get("ACPower")) or _safe_float(inverter.get("power"))
        dc_voltage = _safe_float(inverter.get("DCVoltage")) or _safe_float(inverter.get("dcVoltage"))
        temperature = _safe_float(inverter.get("temperature"))

        return InverterTelemetry(
            manufacturer=self.manufacturer,
            model=str(inverter.get("model") or inverter.get("name") or "SolarEdge"),
            ac_power_w=ac_power,
            dc_voltage_v=dc_voltage,
            temperature_c=temperature,
            update_time=_utcnow(),
        )


class HuaweiSun2000Client(InverterClient):
    """Client for Huawei SUN2000 inverters using the local web API."""

    manufacturer = "Huawei"

    def __init__(self, host: str, timeout: float = 5.0) -> None:
        self.host = host
        self.timeout = timeout

    def read_status(self) -> InverterTelemetry:
        url = f"http://{self.host}/realTimeData.json"
        response = requests.get(url, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()

        ac_power = _safe_float(payload.get("active_power"))
        dc_voltage = _safe_float(payload.get("u_dc"))
        temperature = _safe_float(payload.get("temp"))

        return InverterTelemetry(
            manufacturer=self.manufacturer,
            model=str(payload.get("model") or "SUN2000"),
            ac_power_w=ac_power,
            dc_voltage_v=dc_voltage,
            temperature_c=temperature,
            update_time=_utcnow(),
        )


def poll_devices(
    shelly: ShellyClient,
    inverters: Iterable[InverterClient],
    cycles: int,
    interval_s: float,
) -> None:
    for step in range(cycles):
        shelly_data = shelly.read_status()
        inverter_data = [client.read_status() for client in inverters]

        snapshot = {
            "shelly": _asdict_with_iso(shelly_data),
            "inverters": [_asdict_with_iso(item) for item in inverter_data],
        }

        print(json.dumps(snapshot, indent=2))

        if step + 1 < cycles:
            time.sleep(interval_s)


def _safe_float(value: object) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _lookup(data: Dict[str, object], path: List[str]) -> object:
    cursor: object = data
    for key in path:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(key)
    return cursor


def _asdict_with_iso(obj: object) -> Dict[str, object]:
    if not hasattr(obj, "__dict__"):
        return {}
    result: Dict[str, object] = {}
    for key, value in obj.__dict__.items():
        if isinstance(value, datetime):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _build_inverter_clients(args: argparse.Namespace) -> List[InverterClient]:
    clients: List[InverterClient] = []

    if args.fronius_host:
        clients.append(FroniusClient(args.fronius_host, device_id=args.fronius_device_id))

    if args.solaredge_host:
        clients.append(SolarEdgeClient(args.solaredge_host, api_key=args.solaredge_api_key))

    if args.huawei_host:
        clients.append(HuaweiSun2000Client(args.huawei_host))

    return clients


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read Shelly and inverter data")
    parser.add_argument("--shelly-host", required=True, help="IP or hostname of the Shelly device")
    parser.add_argument("--fronius-host", help="IP or hostname of a Fronius inverter")
    parser.add_argument("--fronius-device-id", type=int, default=1, help="DeviceId for Fronius Solar API")
    parser.add_argument("--solaredge-host", help="IP or hostname of a SolarEdge inverter")
    parser.add_argument("--solaredge-api-key", help="Optional API key for SolarEdge local gateway")
    parser.add_argument("--huawei-host", help="IP or hostname of a Huawei SUN2000 inverter")
    parser.add_argument("--cycles", type=int, default=1, help="Number of polling cycles to run")
    parser.add_argument("--interval", type=float, default=30.0, help="Seconds between polling cycles")
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level for debug output",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level))

    shelly = ShellyClient(args.shelly_host)
    inverter_clients = _build_inverter_clients(args)

    if not inverter_clients:
        logging.warning("No inverter hosts supplied; only Shelly data will be printed")

    poll_devices(
        shelly=shelly,
        inverters=inverter_clients,
        cycles=max(1, args.cycles),
        interval_s=max(0.1, args.interval),
    )


if __name__ == "__main__":
    main()
