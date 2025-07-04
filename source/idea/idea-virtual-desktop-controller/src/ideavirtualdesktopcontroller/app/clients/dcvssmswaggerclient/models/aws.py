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


class Aws(object):
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
        'region': 'str',
        'ec2_instance_type': 'str',
        'ec2_instance_id': 'str',
        'ec2_image_id': 'str',
    }

    attribute_map = {
        'region': 'Region',
        'ec2_instance_type': 'EC2InstanceType',
        'ec2_instance_id': 'EC2InstanceId',
        'ec2_image_id': 'EC2ImageId',
    }

    def __init__(
        self,
        region=None,
        ec2_instance_type=None,
        ec2_instance_id=None,
        ec2_image_id=None,
    ):  # noqa: E501
        """Aws - a model defined in Swagger"""  # noqa: E501
        self._region = None
        self._ec2_instance_type = None
        self._ec2_instance_id = None
        self._ec2_image_id = None
        self.discriminator = None
        if region is not None:
            self.region = region
        if ec2_instance_type is not None:
            self.ec2_instance_type = ec2_instance_type
        if ec2_instance_id is not None:
            self.ec2_instance_id = ec2_instance_id
        if ec2_image_id is not None:
            self.ec2_image_id = ec2_image_id

    @property
    def region(self):
        """Gets the region of this Aws.  # noqa: E501

        The AWS region  # noqa: E501

        :return: The region of this Aws.  # noqa: E501
        :rtype: str
        """
        return self._region

    @region.setter
    def region(self, region):
        """Sets the region of this Aws.

        The AWS region  # noqa: E501

        :param region: The region of this Aws.  # noqa: E501
        :type: str
        """

        self._region = region

    @property
    def ec2_instance_type(self):
        """Gets the ec2_instance_type of this Aws.  # noqa: E501

        The AWS EC2 instance type  # noqa: E501

        :return: The ec2_instance_type of this Aws.  # noqa: E501
        :rtype: str
        """
        return self._ec2_instance_type

    @ec2_instance_type.setter
    def ec2_instance_type(self, ec2_instance_type):
        """Sets the ec2_instance_type of this Aws.

        The AWS EC2 instance type  # noqa: E501

        :param ec2_instance_type: The ec2_instance_type of this Aws.  # noqa: E501
        :type: str
        """

        self._ec2_instance_type = ec2_instance_type

    @property
    def ec2_instance_id(self):
        """Gets the ec2_instance_id of this Aws.  # noqa: E501

        The AWS EC2 instance id  # noqa: E501

        :return: The ec2_instance_id of this Aws.  # noqa: E501
        :rtype: str
        """
        return self._ec2_instance_id

    @ec2_instance_id.setter
    def ec2_instance_id(self, ec2_instance_id):
        """Sets the ec2_instance_id of this Aws.

        The AWS EC2 instance id  # noqa: E501

        :param ec2_instance_id: The ec2_instance_id of this Aws.  # noqa: E501
        :type: str
        """

        self._ec2_instance_id = ec2_instance_id

    @property
    def ec2_image_id(self):
        """Gets the ec2_image_id of this Aws.  # noqa: E501

        The AWS EC2 image id  # noqa: E501

        :return: The ec2_image_id of this Aws.  # noqa: E501
        :rtype: str
        """
        return self._ec2_image_id

    @ec2_image_id.setter
    def ec2_image_id(self, ec2_image_id):
        """Sets the ec2_image_id of this Aws.

        The AWS EC2 image id  # noqa: E501

        :param ec2_image_id: The ec2_image_id of this Aws.  # noqa: E501
        :type: str
        """

        self._ec2_image_id = ec2_image_id

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
        if issubclass(Aws, dict):
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
        if not isinstance(other, Aws):
            return False

        return self.__dict__ == other.__dict__

    def __ne__(self, other):
        """Returns true if both objects are not equal"""
        return not self == other
