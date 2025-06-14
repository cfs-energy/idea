# coding: utf-8

"""
DCV Session Manager

DCV Session Manager API  # noqa: E501

OpenAPI spec version: 2021.3

Generated by: https://github.com/swagger-api/swagger-codegen.git
"""

import pprint
import re  # noqa: F401

import six


class GetSessionScreenshotSuccessfulResponse(object):
    """NOTE: This class is auto generated by the swagger code generator program.

    Do not edit the class manually.
    """

    """
    Attributes:
      swagger_types (dict): The key is attribute name
                            and the value is attribute type.
      attribute_map (dict): The key is attribute name
                            and the value is json key in definition.
    """
    swagger_types = {'session_screenshot': 'SessionScreenshot'}

    attribute_map = {'session_screenshot': 'SessionScreenshot'}

    def __init__(self, session_screenshot=None):  # noqa: E501
        """GetSessionScreenshotSuccessfulResponse - a model defined in Swagger"""  # noqa: E501
        self._session_screenshot = None
        self.discriminator = None
        if session_screenshot is not None:
            self.session_screenshot = session_screenshot

    @property
    def session_screenshot(self):
        """Gets the session_screenshot of this GetSessionScreenshotSuccessfulResponse.  # noqa: E501


        :return: The session_screenshot of this GetSessionScreenshotSuccessfulResponse.  # noqa: E501
        :rtype: SessionScreenshot
        """
        return self._session_screenshot

    @session_screenshot.setter
    def session_screenshot(self, session_screenshot):
        """Sets the session_screenshot of this GetSessionScreenshotSuccessfulResponse.


        :param session_screenshot: The session_screenshot of this GetSessionScreenshotSuccessfulResponse.  # noqa: E501
        :type: SessionScreenshot
        """

        self._session_screenshot = session_screenshot

    def to_dict(self):
        """Returns the model properties as a dict"""
        result = {}

        for attr, _ in six.iteritems(self.swagger_types):
            value = getattr(self, attr)
            if isinstance(value, list):
                result[attr] = list(
                    map(lambda x: x.to_dict() if hasattr(x, 'to_dict') else x, value)
                )
            elif hasattr(value, 'to_dict'):
                result[attr] = value.to_dict()
            elif isinstance(value, dict):
                result[attr] = dict(
                    map(
                        lambda item: (item[0], item[1].to_dict())
                        if hasattr(item[1], 'to_dict')
                        else item,
                        value.items(),
                    )
                )
            else:
                result[attr] = value
        if issubclass(GetSessionScreenshotSuccessfulResponse, dict):
            for key, value in self.items():
                result[key] = value

        return result

    def to_str(self):
        """Returns the string representation of the model"""
        return pprint.pformat(self.to_dict())

    def __repr__(self):
        """For `print` and `pprint`"""
        return self.to_str()

    def __eq__(self, other):
        """Returns true if both objects are equal"""
        if not isinstance(other, GetSessionScreenshotSuccessfulResponse):
            return False

        return self.__dict__ == other.__dict__

    def __ne__(self, other):
        """Returns true if both objects are not equal"""
        return not self == other
