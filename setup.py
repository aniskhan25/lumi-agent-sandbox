from setuptools import find_packages, setup


setup(
    name="lumi-agent-sandbox",
    version="0.1.0",
    description="Small LUMI task sandbox harness for agent workflows",
    packages=find_packages(),
    python_requires=">=3.10",
    entry_points={
        "console_scripts": [
            "lumi-agent-sandbox=lumi_agent_sandbox.cli:main",
        ],
    },
)
