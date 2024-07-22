"""Support for weather service."""
import asyncio
import logging
from datetime import timedelta
from random import randrange
from xml.parsers.expat import ExpatError

from homeassistant.const import (
    MAJOR_VERSION,
    MINOR_VERSION,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)


import aiohttp
import async_timeout
import voluptuous as vol
import xmltodict

from http import HTTPStatus

from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorDeviceClass
from homeassistant.const import (
    ATTR_ATTRIBUTION,
    CONF_ELEVATION,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_MONITORED_CONDITIONS,
    CONF_NAME,
    DEGREE,
    PERCENTAGE,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later, async_track_utc_time_change
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

ATTRIBUTION = (
    "Weather forecast from met.no, delivered by the Norwegian "
    "Meteorological Institute."
)
# https://api.met.no/license_data.html

SENSOR_TYPES = {
    "symbol": ["Symbol", None, None],
    "precipitation": ["Precipitation", "mm", None],
    "temperature": ["Temperature", UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE],
    "windSpeed": ["Wind speed", UnitOfSpeed.METERS_PER_SECOND, None],
    "windGust": ["Wind gust", UnitOfSpeed.METERS_PER_SECOND, None],
    "pressure": ["Pressure", UnitOfPressure.HPA, SensorDeviceClass.PRESSURE],
    "windDirection": ["Wind direction", DEGREE, None],
    "humidity": ["Humidity", PERCENTAGE, SensorDeviceClass.HUMIDITY],
    "fog": ["Fog", PERCENTAGE, None],
    "cloudiness": ["Cloudiness", PERCENTAGE, None],
    "lowClouds": ["Low clouds", PERCENTAGE, None],
    "mediumClouds": ["Medium clouds", PERCENTAGE, None],
    "highClouds": ["High clouds", PERCENTAGE, None],
    "dewpointTemperature": [
        "Dewpoint temperature",
        UnitOfTemperature.CELSIUS,
        SensorDeviceClass.TEMPERATURE,
    ],
}

CONF_FORECAST = "forecast"

DEFAULT_FORECAST = 0
DEFAULT_NAME = "yr"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_ELEVATION): vol.Coerce(int),
        vol.Optional(CONF_FORECAST, default=DEFAULT_FORECAST): vol.Coerce(int),
        vol.Optional(CONF_LATITUDE): cv.latitude,
        vol.Optional(CONF_LONGITUDE): cv.longitude,
        vol.Optional(CONF_MONITORED_CONDITIONS, default=["symbol"]): vol.All(
            cv.ensure_list, vol.Length(min=1), [vol.In(SENSOR_TYPES)]
        ),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the Weather sensor."""
    elevation = config.get(CONF_ELEVATION, hass.config.elevation or 0)
    forecast = config.get(CONF_FORECAST)
    latitude = config.get(CONF_LATITUDE, hass.config.latitude)
    longitude = config.get(CONF_LONGITUDE, hass.config.longitude)
    name = config.get(CONF_NAME)

    if None in (latitude, longitude):
        _LOGGER.error("Latitude or longitude not set in Home Assistant config")
        return False

    coordinates = {
        "lat": str(round(float(latitude), 4)),
        "lon": str(round(float(longitude), 4)),
        "altitude": str(int(float(elevation))),
    }

    dev = []
    for sensor_type in config[CONF_MONITORED_CONDITIONS]:
        dev.append(WeatherSensor(name, sensor_type))

    weather = WeatherData(hass, coordinates, forecast, dev)
    async_track_utc_time_change(
        hass, weather.updating_devices, minute=randrange(60), second=0
    )
    await weather.fetching_data()
    async_add_entities(dev)


class WeatherSensor(Entity):
    """Representation of an weather sensor."""

    def __init__(self, name, sensor_type):
        """Initialize the sensor."""
        self.client_name = name
        self._name = SENSOR_TYPES[sensor_type][0]
        self.type = sensor_type
        self._state = None
        self._unit_of_measurement = SENSOR_TYPES[self.type][1]
        self._device_class = SENSOR_TYPES[self.type][2]

        # changed property name since 2021.12
        if MAJOR_VERSION >= 2022 or (MAJOR_VERSION == 2021 and MINOR_VERSION == 12):
            WeatherSensor.extra_state_attributes = property(lambda self: {ATTR_ATTRIBUTION: ATTRIBUTION})
        else:
            WeatherSensor.device_state_attributes = property(lambda self: {ATTR_ATTRIBUTION: ATTRIBUTION})

    @property
    def name(self):
        """Return the name of the sensor."""
        return f"{self.client_name} {self._name}"

    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def entity_picture(self):
        """Weather symbol if type is symbol."""
        if self.type != "symbol":
            return None
        return "https://api.met.no/images/weathericons/" f"png/{self._state}.png"

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement of this entity, if any."""
        return self._unit_of_measurement

    @property
    def device_class(self):
        """Return the device class of this entity, if any."""
        return self._device_class


class WeatherData:
    """Get the latest data and updates the states."""

    def __init__(self, hass, coordinates, forecast, devices):
        """Initialize the data object."""
        # Dedicated Home Assistant endpoint - do not change!
        self._url = "https://aa015h6buqvih86i1.api.met.no/weatherapi/locationforecast/2.0/complete"
        # self._url = "https://api.met.no/weatherapi/locationforecast/2.0/classic"
        self._urlparams = coordinates
        self._forecast = forecast
        self.devices = devices
        self.data = {}
        self.hass = hass

    async def fetching_data(self, *_):
        """Get the latest data from met.no."""

        def try_again(err: str):
            """Retry in 15 to 20 minutes."""
            minutes = 15 + randrange(6)
            _LOGGER.error("Retrying in %i minutes: %s", minutes, err)
            async_call_later(self.hass, minutes * 60, self.fetching_data)

        try:
            websession = async_get_clientsession(self.hass)
            with async_timeout.timeout(10):
                resp = await websession.get(self._url, params=self._urlparams)
            if resp.status >= HTTPStatus.BAD_REQUEST:
                try_again(f"{resp.url} returned {resp.status}")
                return
            json = await resp.json()

        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            try_again(err)
            return

        try:
            self.data = json
        except (ExpatError, IndexError) as err:
            try_again(err)
            return

        await self.updating_devices()
        async_call_later(self.hass, 60 * 60, self.fetching_data)

    async def updating_devices(self, *_):
        """Find the current data from self.data."""
        if not self.data:
            return

        now = dt_util.utcnow()
        forecast_time = now + dt_util.dt.timedelta(hours=self._forecast)

        # Find the correct time entry. Since not all time entries contain all
        # types of data, we cannot just select one. Instead, we order  them by
        # distance from the desired forecast_time, and for every device iterate
        # them in order of increasing distance, taking the first time_point
        # that contains the desired data.

        ordered_entries = []

        for time_entry in self.data["properties"]["timeseries"]:
            valid_from = dt_util.parse_datetime(time_entry["time"])
            valid_to = valid_from + dt_util.dt.timedelta(hours=1)

            if now >= valid_to:
                # Has already passed. Never select this.
                continue

            average_dist = abs((valid_to - forecast_time).total_seconds()) + abs(
                (valid_from - forecast_time).total_seconds()
            )

            ordered_entries.append((average_dist, time_entry))

        ordered_entries.sort(key=lambda item: item[0])

        # Update all devices
        if ordered_entries:
            for dev in self.devices:
                new_state = None

                for (_, selected_time_entry) in ordered_entries:
                    try:
                        instant = selected_time_entry["data"]["instant"]["details"]
                        hour = selected_time_entry["data"]["next_1_hours"]["details"]
                    except KeyError:
                        continue

                    if dev.type == "symbol":
                        try:
                            new_state = selected_time_entry["data"]["next_1_hours"]["summary"]["symbol_code"]
                        except KeyError:
                            continue

                    elif dev.type == "precipitation":
                        try:
                            new_state = hour["precipitation_amount"]
                        except KeyError:
                            continue

                    elif dev.type == "temperature":
                        try:
                            new_state = instant["air_temperature"]
                        except KeyError:
                            continue

                    elif dev.type == "pressure":
                        try:
                            new_state = instant["air_pressure_at_sea_level"]
                        except KeyError:
                            continue

                    elif dev.type == "humidity":
                        try:
                            new_state = instant["relative_humidity"]
                        except KeyError:
                            continue

                    elif dev.type == "dewpointTemperature":
                        try:
                            new_state = instant["dew_point_temperature"]
                        except KeyError:
                            continue

                    elif dev.type == "windSpeed":
                        try:
                            new_state = instant["wind_speed"]
                        except KeyError:
                            continue

                    elif dev.type == "windGust":
                        try:
                            new_state = instant["wind_speed_of_gust"]
                        except KeyError:
                            continue

                    elif dev.type == "windDirection":
                        try:
                            new_state = instant["wind_from_direction"]
                        except KeyError:
                            continue

                    elif dev.type == "fog":
                        try:
                            new_state = instant["fog_area_fraction"]
                        except KeyError:
                            continue
                    elif dev.type == "cloudiness":
                        try:
                            new_state = instant["cloud_area_fraction"]
                        except KeyError:
                            continue
                    elif dev.type == "lowClouds":
                        try:
                            new_state = instant["cloud_area_fraction_low"]
                        except KeyError:
                            continue
                    elif dev.type == "mediumClouds":
                        try:
                            new_state = instant["cloud_area_fraction_medium"]
                        except KeyError:
                            continue
                    elif dev.type == "highClouds":
                        try:
                            new_state = instant["cloud_area_fraction_high"]
                        except KeyError:
                            continue

                    break

                # pylint: disable=protected-access
                if new_state != dev._state:
                    dev._state = new_state
                    if dev.hass:
                        dev.async_write_ha_state()
