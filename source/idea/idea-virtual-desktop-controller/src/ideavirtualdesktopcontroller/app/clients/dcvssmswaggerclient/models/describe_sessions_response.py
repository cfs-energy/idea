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


class DescribeSessionsResponse(object):
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
        'request_id': 'str',
        'sessions': 'list[Session]',
        'next_token': 'str',
    }

    attribute_map = {
        'request_id': 'RequestId',
        'sessions': 'Sessions',
        'next_token': 'NextToken',
    }

    def __init__(self, request_id=None, sessions=None, next_token=None):  # noqa: E501
        """DescribeSessionsResponse - a model defined in Swagger"""  # noqa: E501
        self._request_id = None
        self._sessions = None
        self._next_token = None
        self.discriminator = None
        if request_id is not None:
            self.request_id = request_id
        if sessions is not None:
            self.sessions = sessions
        if next_token is not None:
            self.next_token = next_token

    @property
    def request_id(self):
        """Gets the request_id of this DescribeSessionsResponse.  # noqa: E501

        DescribeSessions request id  # noqa: E501

        :return: The request_id of this DescribeSessionsResponse.  # noqa: E501
        :rtype: str
        """
        return self._request_id

    @request_id.setter
    def request_id(self, request_id):
        """Sets the request_id of this DescribeSessionsResponse.

        DescribeSessions request id  # noqa: E501

        :param request_id: The request_id of this DescribeSessionsResponse.  # noqa: E501
        :type: str
        """

        self._request_id = request_id

    @property
    def sessions(self):
        """Gets the sessions of this DescribeSessionsResponse.  # noqa: E501

        The array of sessions described  # noqa: E501

        :return: The sessions of this DescribeSessionsResponse.  # noqa: E501
        :rtype: list[Session]
        """
        return self._sessions

    @sessions.setter
    def sessions(self, sessions):
        """Sets the sessions of this DescribeSessionsResponse.

        The array of sessions described  # noqa: E501

        :param sessions: The sessions of this DescribeSessionsResponse.  # noqa: E501
        :type: list[Session]
        """

        self._sessions = sessions

    @property
    def next_token(self):
        """Gets the next_token of this DescribeSessionsResponse.  # noqa: E501

        The token used for pagination  # noqa: E501

        :return: The next_token of this DescribeSessionsResponse.  # noqa: E501
        :rtype: str
        """
        return self._next_token

    @next_token.setter
    def next_token(self, next_token):
        """Sets the next_token of this DescribeSessionsResponse.

        The token used for pagination  # noqa: E501

        :param next_token: The next_token of this DescribeSessionsResponse.  # noqa: E501
        :type: str
        """

        self._next_token = next_token

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
        if issubclass(DescribeSessionsResponse, dict):
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
        if not isinstance(other, DescribeSessionsResponse):
            return False

        return self.__dict__ == other.__dict__

    def __ne__(self, other):
        """Returns true if both objects are not equal"""
        return not self == other
