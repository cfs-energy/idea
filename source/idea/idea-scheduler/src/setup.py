from setuptools import setup, find_packages
import ideascheduler_meta

setup(
    name=ideascheduler_meta.__name__,
    version=ideascheduler_meta.__version__,
    description='Scale-Out Computing',
    url='https://awslabs.github.io/scale-out-computing-on-aws/',
    author='Amazon',
    license='Apache License, Version 2.0',
    packages=find_packages(),
    package_dir={
        'ideascheduler': 'ideascheduler'
    },
    entry_points='''
        [console_scripts]
        ideaserver=ideascheduler.app.app_main:main
        ideactl=ideascheduler.cli.cli_main:main
    '''
)
