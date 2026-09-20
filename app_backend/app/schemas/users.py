from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import field_validator
from .common import ID, Name, Schema, Record


class NewDogUser(Schema):
    name: Name
    timezone: str = 'America/New_York'

    @field_validator('timezone')
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('An IANA timezone is required')
        return value


class DogUser(NewDogUser, Record):
    pass


class NewAppUser(Schema):
    name: Name
    dog_user_id: ID


class AppUser(NewAppUser, Record):
    pass
