from setuptools import setup, find_packages

setup(
    name="bifrost-python-lib",
    version="v0.2.0",
    package=find_packages,
    install_requires=[
        "git+https://github.com/bifrost-platform/bifrost-python-lib.git"
    ]
)
