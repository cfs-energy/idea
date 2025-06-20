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


class CpuInfo(object):
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
        'vendor': 'str',
        'model_name': 'str',
        'architecture': 'str',
        'number_of_cpus': 'int',
        'physical_cores_per_cpu': 'int',
    }

    attribute_map = {
        'vendor': 'Vendor',
        'model_name': 'ModelName',
        'architecture': 'Architecture',
        'number_of_cpus': 'NumberOfCpus',
        'physical_cores_per_cpu': 'PhysicalCoresPerCpu',
    }

    def __init__(
        self,
        vendor=None,
        model_name=None,
        architecture=None,
        number_of_cpus=None,
        physical_cores_per_cpu=None,
    ):  # noqa: E501
        """CpuInfo - a model defined in Swagger"""  # noqa: E501
        self._vendor = None
        self._model_name = None
        self._architecture = None
        self._number_of_cpus = None
        self._physical_cores_per_cpu = None
        self.discriminator = None
        if vendor is not None:
            self.vendor = vendor
        if model_name is not None:
            self.model_name = model_name
        if architecture is not None:
            self.architecture = architecture
        if number_of_cpus is not None:
            self.number_of_cpus = number_of_cpus
        if physical_cores_per_cpu is not None:
            self.physical_cores_per_cpu = physical_cores_per_cpu

    @property
    def vendor(self):
        """Gets the vendor of this CpuInfo.  # noqa: E501

        The vendor of the cpu  # noqa: E501

        :return: The vendor of this CpuInfo.  # noqa: E501
        :rtype: str
        """
        return self._vendor

    @vendor.setter
    def vendor(self, vendor):
        """Sets the vendor of this CpuInfo.

        The vendor of the cpu  # noqa: E501

        :param vendor: The vendor of this CpuInfo.  # noqa: E501
        :type: str
        """

        self._vendor = vendor

    @property
    def model_name(self):
        """Gets the model_name of this CpuInfo.  # noqa: E501

        The model name of the cpu  # noqa: E501

        :return: The model_name of this CpuInfo.  # noqa: E501
        :rtype: str
        """
        return self._model_name

    @model_name.setter
    def model_name(self, model_name):
        """Sets the model_name of this CpuInfo.

        The model name of the cpu  # noqa: E501

        :param model_name: The model_name of this CpuInfo.  # noqa: E501
        :type: str
        """

        self._model_name = model_name

    @property
    def architecture(self):
        """Gets the architecture of this CpuInfo.  # noqa: E501

        The architecture of the cpu  # noqa: E501

        :return: The architecture of this CpuInfo.  # noqa: E501
        :rtype: str
        """
        return self._architecture

    @architecture.setter
    def architecture(self, architecture):
        """Sets the architecture of this CpuInfo.

        The architecture of the cpu  # noqa: E501

        :param architecture: The architecture of this CpuInfo.  # noqa: E501
        :type: str
        """

        self._architecture = architecture

    @property
    def number_of_cpus(self):
        """Gets the number_of_cpus of this CpuInfo.  # noqa: E501

        The number of cpus  # noqa: E501

        :return: The number_of_cpus of this CpuInfo.  # noqa: E501
        :rtype: int
        """
        return self._number_of_cpus

    @number_of_cpus.setter
    def number_of_cpus(self, number_of_cpus):
        """Sets the number_of_cpus of this CpuInfo.

        The number of cpus  # noqa: E501

        :param number_of_cpus: The number_of_cpus of this CpuInfo.  # noqa: E501
        :type: int
        """

        self._number_of_cpus = number_of_cpus

    @property
    def physical_cores_per_cpu(self):
        """Gets the physical_cores_per_cpu of this CpuInfo.  # noqa: E501

        The physical core count per cpu  # noqa: E501

        :return: The physical_cores_per_cpu of this CpuInfo.  # noqa: E501
        :rtype: int
        """
        return self._physical_cores_per_cpu

    @physical_cores_per_cpu.setter
    def physical_cores_per_cpu(self, physical_cores_per_cpu):
        """Sets the physical_cores_per_cpu of this CpuInfo.

        The physical core count per cpu  # noqa: E501

        :param physical_cores_per_cpu: The physical_cores_per_cpu of this CpuInfo.  # noqa: E501
        :type: int
        """

        self._physical_cores_per_cpu = physical_cores_per_cpu

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
        if issubclass(CpuInfo, dict):
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
        if not isinstance(other, CpuInfo):
            return False

        return self.__dict__ == other.__dict__

    def __ne__(self, other):
        """Returns true if both objects are not equal"""
        return not self == other
