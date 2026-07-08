#!/usr/bin/env python3
"""Python 3.6-compatible setuptools metadata for CamFlow."""
from __future__ import print_function

from setuptools import find_packages, setup


setup(
    name="camflow",
    version="1.2.0",
    description="Thin prompt-call-verify-trace runner for static v1.2 workflows",
    long_description=open("README.md", "r").read(),
    long_description_content_type="text/markdown",
    python_requires=">=3.6",
    package_dir={"": "src"},
    packages=find_packages("src", include=["camflow_pkg", "camflow_pkg.*"]),
    entry_points={"console_scripts": ["camflow=camflow_pkg.cli:main"]},
)
