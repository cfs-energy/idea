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


class UnsuccessfulCreateSessionRequestData(object):
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
    swagger_types = {
        'create_session_request_data': 'CreateSessionRequestData',
        'failure_reason': 'str',
    }

    attribute_map = {
        'create_session_request_data': 'CreateSessionRequestData',
        'failure_reason': 'FailureReason',
    }

    def __init__(self, create_session_request_data=None, failure_reason=None):  # noqa: E501
        """UnsuccessfulCreateSessionRequestData - a model defined in Swagger"""  # noqa: E501
        self._create_session_request_data = None
        self._failure_reason = None
        self.discriminator = None
        if create_session_request_data is not None:
            self.create_session_request_data = create_session_request_data
        if failure_reason is not None:
            self.failure_reason = failure_reason

    @property
    def create_session_request_data(self):
        """Gets the create_session_request_data of this UnsuccessfulCreateSessionRequestData.  # noqa: E501


        :return: The create_session_request_data of this UnsuccessfulCreateSessionRequestData.  # noqa: E501
        :rtype: CreateSessionRequestData
        """
        return self._create_session_request_data

    @create_session_request_data.setter
    def create_session_request_data(self, create_session_request_data):
        """Sets the create_session_request_data of this UnsuccessfulCreateSessionRequestData.


        :param create_session_request_data: The create_session_request_data of this UnsuccessfulCreateSessionRequestData.  # noqa: E501
        :type: CreateSessionRequestData
        """

        self._create_session_request_data = create_session_request_data

    @property
    def failure_reason(self):
        """Gets the failure_reason of this UnsuccessfulCreateSessionRequestData.  # noqa: E501

        The failure reason  # noqa: E501

        :return: The failure_reason of this UnsuccessfulCreateSessionRequestData.  # noqa: E501
        :rtype: str
        """
        return self._failure_reason

    @failure_reason.setter
    def failure_reason(self, failure_reason):
        """Sets the failure_reason of this UnsuccessfulCreateSessionRequestData.

        The failure reason  # noqa: E501

        :param failure_reason: The failure_reason of this UnsuccessfulCreateSessionRequestData.  # noqa: E501
        :type: str
        """

        self._failure_reason = failure_reason

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
        if issubclass(UnsuccessfulCreateSessionRequestData, dict):
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
        if not isinstance(other, UnsuccessfulCreateSessionRequestData):
            return False

        return self.__dict__ == other.__dict__

    def __ne__(self, other):
        """Returns true if both objects are not equal"""
        return not self == other
