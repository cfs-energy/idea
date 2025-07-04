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


class CreateSessionsResponse(object):
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
        'successful_list': 'list[Session]',
        'unsuccessful_list': 'list[UnsuccessfulCreateSessionRequestData]',
    }

    attribute_map = {
        'request_id': 'RequestId',
        'successful_list': 'SuccessfulList',
        'unsuccessful_list': 'UnsuccessfulList',
    }

    def __init__(self, request_id=None, successful_list=None, unsuccessful_list=None):  # noqa: E501
        """CreateSessionsResponse - a model defined in Swagger"""  # noqa: E501
        self._request_id = None
        self._successful_list = None
        self._unsuccessful_list = None
        self.discriminator = None
        if request_id is not None:
            self.request_id = request_id
        if successful_list is not None:
            self.successful_list = successful_list
        if unsuccessful_list is not None:
            self.unsuccessful_list = unsuccessful_list

    @property
    def request_id(self):
        """Gets the request_id of this CreateSessionsResponse.  # noqa: E501

        CreateSessions request id  # noqa: E501

        :return: The request_id of this CreateSessionsResponse.  # noqa: E501
        :rtype: str
        """
        return self._request_id

    @request_id.setter
    def request_id(self, request_id):
        """Sets the request_id of this CreateSessionsResponse.

        CreateSessions request id  # noqa: E501

        :param request_id: The request_id of this CreateSessionsResponse.  # noqa: E501
        :type: str
        """

        self._request_id = request_id

    @property
    def successful_list(self):
        """Gets the successful_list of this CreateSessionsResponse.  # noqa: E501

        The array of sessions that are in process of being created  # noqa: E501

        :return: The successful_list of this CreateSessionsResponse.  # noqa: E501
        :rtype: list[Session]
        """
        return self._successful_list

    @successful_list.setter
    def successful_list(self, successful_list):
        """Sets the successful_list of this CreateSessionsResponse.

        The array of sessions that are in process of being created  # noqa: E501

        :param successful_list: The successful_list of this CreateSessionsResponse.  # noqa: E501
        :type: list[Session]
        """

        self._successful_list = successful_list

    @property
    def unsuccessful_list(self):
        """Gets the unsuccessful_list of this CreateSessionsResponse.  # noqa: E501

        The array of sessions that cannot be created  # noqa: E501

        :return: The unsuccessful_list of this CreateSessionsResponse.  # noqa: E501
        :rtype: list[UnsuccessfulCreateSessionRequestData]
        """
        return self._unsuccessful_list

    @unsuccessful_list.setter
    def unsuccessful_list(self, unsuccessful_list):
        """Sets the unsuccessful_list of this CreateSessionsResponse.

        The array of sessions that cannot be created  # noqa: E501

        :param unsuccessful_list: The unsuccessful_list of this CreateSessionsResponse.  # noqa: E501
        :type: list[UnsuccessfulCreateSessionRequestData]
        """

        self._unsuccessful_list = unsuccessful_list

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
        if issubclass(CreateSessionsResponse, dict):
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
        if not isinstance(other, CreateSessionsResponse):
            return False

        return self.__dict__ == other.__dict__

    def __ne__(self, other):
        """Returns true if both objects are not equal"""
        return not self == other
