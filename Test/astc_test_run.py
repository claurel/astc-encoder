#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# -----------------------------------------------------------------------------
# Copyright 2019-2020 Arm Limited
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at:
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
# -----------------------------------------------------------------------------

import argparse
import csv
import junit_xml as juxml
import os
from PIL import Image
import re
import shutil
import subprocess as sp
import sys


LOG_CLI = False

TEST_BLOCK_SIZES = ["4x4", "5x5", "6x6", "8x8", "12x12",
                    "3x3x3", "6x6x6"]
TEST_EXTENSIONS = [".png", ".hdr", ".dds"]

class TestReference():
    """
    A single test reference result from the reference spreadsheet. These
    define the baseline for pass/fail for both PSNR and performance.
    """

    def __init__(self, row):
        self.name = row[0]
        self.testBlock = row[1]
        self.testPSNR = row[2]
        self.testTime = row[3]


class TestImage():
    """
    A single test definition, and the test results if it is actually run.
    """
    warmupRuns = 0
    testRuns = 1

    def __init__(self, filePath, testReference, testSet, patchRun=False):
        """
        Construct a new test definition.
        """
        self.filePath = filePath
        self.testSet = testSet

        # Name is the file name minus any extension (strip flags later)
        self.name = os.path.basename(self.filePath)[:-4]

        # All tests are used in the full run
        self.useLevel = ["all"]
        self.useFormat = ["all"]
        self.useRange = ["all"]
        self.is2D = True

        # Tokenize the file name
        nameParts = self.name.split("-")
        if len(nameParts) == 4:
            assert len(nameParts) == 4
            if "s" in nameParts[3]:
                self.useLevel.append("smoke")
            if "x" in nameParts[3]:
                self.useLevel = []
            if "3" in nameParts[3]:
                self.is2D = False

            # Name of the test excludes flags from the file name
            self.name = "-".join(nameParts[0:3])
        else:
            assert len(nameParts) == 3

        self.dynamicRange = nameParts[0]
        self.useRange.append(self.dynamicRange)

        self.format = nameParts[1]
        self.useFormat.append(self.format)

        # Find the reference data for this test in the spreadsheet
        self.referencePSNR = dict()
        self.referenceTime = dict()

        if testReference:
            for ref in testReference:
                if ref.name == self.name:
                    self.referencePSNR[ref.testBlock] = float(ref.testPSNR)
                    self.referenceTime[ref.testBlock] = float(ref.testTime)

            # Sanity check we found some results
            if not patchRun:
                assert self.referencePSNR, "Reference scores not found"

        # Initialize test run results
        self.runTime = dict()
        self.runPSNR = dict()
        self.status = dict()

    def run_once(self, testBinary, blockSize, firstRun, rebuild):
        """
        Run a single compression pass.
        """
        pathParts = splitall(self.filePath)
        assert len(pathParts) == 4

        # Create the test output directory if it doesn't exist
        outDir = os.path.join("TestOutput", self.testSet,
                              pathParts[2], blockSize)
        os.makedirs(outDir,  exist_ok=True)

        if self.dynamicRange == "ldr":
            outFile = pathParts[3].replace(".png", ".tga")
        else:
            outFile = pathParts[3].replace(".hdr", ".htga")
        outFilePath = os.path.join(outDir, outFile)

        if self.dynamicRange == "ldr":
            outFile = pathParts[3].replace(".png", "-out.png")
            outFilePath2 = os.path.join(outDir, outFile)

        # Switch sRGB images into sRGB mode
        if self.format in ("srgb", "srgba"):
            opmode = "-ts"
        elif self.dynamicRange == "ldr":
            opmode = "-tl"
        else:
            opmode = "-t"

        # Run the compressor
        args = [testBinary, opmode, self.filePath, outFilePath,
                blockSize, "-thorough", "-silent"]

        # Switch normal maps into angular error metrics
        if self.format == "xy":
            args.append("-normal_psnr")

        # Switch HDR data formats into HDR compression mode; note that this
        # mode assumes that the alpha channel is non-correlated
        if self.dynamicRange == "hdr":
            args.append("-hdr")

        if LOG_CLI:
            print(" + %s " % " ".join(args))

        # For reference runs we need to translate the command line back
        # to the old format
        if rebuild:
            args = self.rewrite_args_for_old_cli(args)

        try:
            result = sp.run(args, stdout=sp.PIPE, stderr=sp.PIPE,
                            check=True, universal_newlines=True)
        except (OSError, sp.CalledProcessError):
            print("ERROR: Test run failed")
            print("  + %s" % " ".join(args))
            sys.exit(1)

        # Convert the TGA to PNG and delete the TGA (LDR only)
        if self.dynamicRange == "ldr" and self.is2D:
            im = Image.open(outFilePath)
            im.save(outFilePath2)
            os.remove(outFilePath)

        # TODO: Convert the HTGA to EXR or HDR (HDR only)

        # Create log parsing patterns
        if self.dynamicRange == "ldr":
            if self.format in ("rgb", "xy", "l"):
                patternPSNR = r"PSNR \(LDR-RGB\):\s*([0-9.]*) dB"
            elif self.format in ("srgba", "rgba"):
                patternPSNR = r"PSNR \(LDR-RGBA\):\s*([0-9.]*) dB"
            else:
                assert False, "Unsupported LDR color format %s" % self.format
        else:
            patternPSNR = r"mPSNR \(RGB\)(?: \[.*?\] )?:\s*([0-9.]*) dB.*"

        patternPSNR = re.compile(patternPSNR)
        patternTime = re.compile(".*[Cc]oding time:\s*([0-9.]*) s.*")

        # Extract results from the log
        runPSNR = None
        runTime = None

        for line in result.stdout.splitlines():
            match = patternPSNR.match(line)
            if match:
                runPSNR = float(match.group(1))

            match = patternTime.match(line)
            if match:
                runTime = float(match.group(1))

        assert runPSNR is not None, "No coding PSNR found %s" % result.stdout
        assert runTime is not None, "No coding time found %s" % result.stdout

        return (runPSNR, runTime)

    def rewrite_args_for_old_cli(self, args):
        replacements = [
            ("-silent", "-silentmode")
        ]

        extensions = [
            ("-t", ("-showpsnr", "-time")),
            ("-tl", ("-showpsnr", "-time")),
            ("-ts", ("-showpsnr", "-time"))
        ]

        for new, old in replacements:
            args = [old if x == new else x for x in args]

        for new, exts in extensions:
            if new in args:
                args.extend(exts)

        return args

    def run(self, testBinary, blockSize, failureDiff, rebuild=False):
        """
        Run the test scenario including N warmup passes and M run passes.

        Returned performance score is the average of the M run passes.
        """
        results = []
        for i in range(0, self.warmupRuns):
            self.run_once(testBinary, blockSize, False, rebuild)

        for i in range(0, self.testRuns):
            result = self.run_once(testBinary, blockSize, i == 0, rebuild)
            results.append(result)

        listPSNR, timeList = list(zip(*results))

        # Store raw results
        self.runPSNR[blockSize] = listPSNR[0]
        self.runTime[blockSize] = sum(timeList) / len(timeList)

        # No reference data is a failure
        if blockSize not in self.referencePSNR:
            self.status[blockSize] = "fail"
            return

        refPSNR = float(self.referencePSNR[blockSize])
        diffPSNR = listPSNR[0] - refPSNR

        refTime = float(self.referenceTime[blockSize])
        speedup = ((refTime / self.runTime[blockSize]) - 1.0) * 100.0

        # Pass if PSNR is better or above threshold
        if (listPSNR[0] >= refPSNR) or (diffPSNR >= failureDiff):
            self.status[blockSize] = "pass | PSNR % 0.3f dB" % diffPSNR
        # Else we got worse by at least threshold so it's a fail ...
        else:
            self.status[blockSize] = "fail | PSNR % 0.3f dB" % diffPSNR

        self.status[blockSize] += " | Speedup % 0.1f%%" % speedup

    def skip_run(self, blockSize):
        """
        Skip the test scenario, but propagate results from reference
        """
        self.runPSNR[blockSize] = self.referencePSNR[blockSize]
        self.runTime[blockSize] = self.referenceTime[blockSize]


def splitall(path):
    """
    Completely tokenize a path into its component pieces.
    """
    allparts = []
    while True:
        parts = os.path.split(path)
        if parts[0] == path:
            allparts.insert(0, parts[0])
            break
        elif parts[1] == path:
            allparts.insert(0, parts[1])
            break
        else:
            path = parts[0]
            allparts.insert(0, parts[1])

    return allparts


def get_test_binary():
    """
    Return the test binary path for the current host machine.
    """
    if "linux" in sys.platform:
        return "./Source/astcenc"
    elif sys.platform == "darwin":
        return "./Source/astcenc"
    elif sys.platform == "win32":
        return "./Source/VS2017/Release/astcenc.exe"

    assert False, "Unknown operating system %s" % sys.platform


def get_reference_binary():
    """
    Return the reference binary path for the current host machine.
    """
    if "linux" in sys.platform:
        return "./Binary/linux-x64/astcenc"
    elif sys.platform == "darwin":
        return "./Binary/mac-x64/astcenc"
    elif sys.platform == "win32":
        return "./Binary/windows-x64/astcenc.exe"

    assert False, "Unknown operating system %s" % sys.platform


def get_test_listing(testReference, testSet, patchRun=False):
    """
    Return the test image listing.
    """
    tests = []
    for root, _, files, in os.walk(os.path.join("Test", testSet)):
        for testFile in files:
            # Detect test images
            for ext in TEST_EXTENSIONS:
                if testFile.endswith(ext):
                    break
            else:
                continue

            testFilePath = os.path.join(root, testFile)
            image = TestImage(testFilePath, testReference, testSet, patchRun)

            # If this image has any test use level then add it to the list
            if image.useLevel:
                tests.append(image)

    return tests


def get_test_reference_scores(testRefFile):
    """
    Return the test reference score listing.
    """
    referenceResults = []
    with open(testRefFile) as csvfile:
        reader = csv.reader(csvfile)
        next(reader)
        for row in reader:
            result = TestReference(row)
            referenceResults.append(result)

    return referenceResults


def run_tests(args, testSet, testRef, failureDiff):
    """
    Run the user defined test scenario.
    """
    TestImage.testRuns = args.testRepeats
    TestImage.warmupRuns = args.testWarmups

    # Create output location
    if not os.path.exists("TestOutput"):
        os.mkdir("TestOutput")

    # Load test resources
    binary = get_test_binary()
    reference = get_test_reference_scores(testRef)
    testList = get_test_listing(reference, testSet)

    # Run tests
    suites = []
    suite = None
    suiteFormat = None

    statRun = 0
    statSkip = 0
    statPass = 0

    # Build a list of valid pairings of block size and test
    tests = []
    for blockSize in TEST_BLOCK_SIZES:
        for test in testList:
            is2DBlock = (blockSize.count("x") == 1)
            is2DTest = test.is2D
            if is2DBlock == is2DTest:
                tests.append((blockSize, test))

    maxCount = len(tests)
    curCount = 0

    for blockSize, test in tests:
        curCount += 1

        # Skip tests not enabled for the current testing throughness level
        if args.testLevel not in test.useLevel:
            statSkip += 1
            continue

        # Skip tests not enabled for the current dynamic range level
        if args.testRange not in test.useRange:
            statSkip += 1
            continue

        # Skip tests not enabled for the current data format
        if args.testFormat not in test.useFormat:
            statSkip += 1
            continue

        # Start a new suite if the format changes
        dat = (test.dynamicRange, test.format, blockSize)
        testFormat = "%s.%s.%s" % dat
        if (not suite) or (suiteFormat != testFormat):
            suiteFormat = testFormat
            suite = juxml.TestSuite("Image %s test suite" % suiteFormat)
            suites.append(suite)
            print("Running suite: %s" % suiteFormat)

        # Run the test
        test.run(binary, blockSize, failureDiff)
        dat = (curCount, maxCount, test.name, blockSize,
                test.runPSNR[blockSize], test.runTime[blockSize],
                test.status[blockSize])

        # Log results
        statRun += 1
        if "pass" in test.status[blockSize]:
            statPass += 1

        log = "Ran %2u/%2u: %s %s, %0.3f dB, %0.3f s, %s" % dat
        print(" + %s" % log)

        # Generate JUnit result
        caseName = "%s.%s" % (test.name, blockSize)
        case = juxml.TestCase(caseName,
                                elapsed_sec=test.runTime[blockSize],
                                stdout=log)
        suite.test_cases.append(case)

        if test.status[blockSize] == "fail":
            dat = (test.runPSNR[blockSize], test.referencePSNR[blockSize])
            msg = "PSNR fail %0.3f dB is worse than %s dB" % dat
            case.add_failure_info(msg)

    # Print summary results
    print("\nSummary")
    if statRun == statPass:
        print("+ PASS (%u ran)" % statRun)
    else:
        print("+ FAIL (%u ran, %u failed)" % (statRun, statRun - statPass))

    # Write the JUnit results file
    with open("TestOutput/results.xml", "w") as fileHandle:
        juxml.TestSuite.to_file(fileHandle, suites)



def run_rebuild(binary, testList, canSkip):
    # Build a list of valid pairings of block size and test
    tests = []
    for blockSize in TEST_BLOCK_SIZES:
        for test in testList:
            is2DBlock = (blockSize.count("x") == 1)
            is2DTest = test.is2D
            if is2DBlock == is2DTest:
                tests.append((blockSize, test))

    curCount = 0
    maxCount = len(tests)

    for blockSize, test in tests:
        curCount += 1

        if canSkip and (blockSize in test.referencePSNR):
            dat = (curCount, maxCount, test.name, blockSize)
            print("Skipping %u/%u: %s @ %s" % dat)
            test.skip_run(blockSize)
        else:
            # Run the test
            dat = (curCount, maxCount, test.name, blockSize)
            print("Running %u/%u: %s @ %s" % dat)
            test.run(binary, blockSize, 0, True)

        runPSNR = "%0.3f" % test.runPSNR[blockSize]
        runTime = "%0.3f" % test.runTime[blockSize]
        print("  + %s dB / %s s" % (runPSNR, runTime))


def run_reference_rebuild(args, testSet, testRef, canSkip=False):
    """
    Run the reference test generator rebuild process.
    """
    TestImage.testRuns = args.testRepeats
    TestImage.warmupRuns = args.testWarmups

    # Delete and recreate test output location
    if os.path.exists("TestOutput"):
        shutil.rmtree("TestOutput")
    os.mkdir("TestOutput")

    # Load test resources
    binary = get_reference_binary()
    refData = None
    if canSkip:
        refData = get_test_reference_scores(testRef)
    testList = get_test_listing(refData, testSet, refData != None)

    run_rebuild(binary, testList, canSkip)

    # Write CSV
    with open(testRef, "w", newline="") as fileHandle:
        writer = csv.writer(fileHandle)
        writer.writerow(["Name", "Block Size", "PSNR (dB)", "Time (s)"])
        for blockSize in TEST_BLOCK_SIZES:
            for test in testList:
                is2DBlock = (blockSize.count("x") == 1)
                is2DTest = test.is2D
                if is2DBlock != is2DTest:
                    continue

                row = (test.name, blockSize,
                    "%0.3f" % test.runPSNR[blockSize],
                    "%0.3f" % test.runTime[blockSize])

                writer.writerow(row)


def parse_command_line():
    """
    Parse the command line.
    """
    parser = argparse.ArgumentParser()

    parser.add_argument("--test-level", dest="testLevel", default="smoke",
                        choices=["smoke", "all"],
                        help="testing test level")

    parser.add_argument("--dynamic-range", dest="testRange", default="all",
                        choices=["ldr", "hdr", "all"],
                        help="testing dynamic range")

    parser.add_argument("--format", dest="testFormat", default="all",
                        choices=["xy", "rgb", "rgba", "all"],
                        help="testing dynamic range")

    choices = list(TEST_BLOCK_SIZES) + ["all"]
    parser.add_argument("--block-size", dest="testBlockSize", default="all",
                        choices=choices, help="testing block size")

    choices = ("Kodak_Images", "Small_Images")
    parser.add_argument("--test-set", dest="testSet", default="Small_Images",
                        choices=choices, help="testing image size")

    parser.add_argument("--repeats", dest="testRepeats", default=1,
                        type=int, help="test iteration count")

    parser.add_argument("--warmup", dest="testWarmups", default=0,
                        type=int, help="test warmup count")

    parser.add_argument("--rebuild-ref-csv", default=False, dest="refRebuild",
                        action="store_true", help="rebuild reference data")

    parser.add_argument("--update-ref-csv", default=False, dest="refUpdate",
                        action="store_true", help="update reference data")

    parser.add_argument("--fail-threshold", default=-0.1, dest="failureDiff",
                        type=float, help="failure threshold (dB)")

    args = parser.parse_args()

    if args.testBlockSize == "all":
        args.testBlockSize = TEST_BLOCK_SIZES
    else:
        args.testBlockSize = [args.testBlockSize]

    return args


def main():
    """
    The main function.
    """
    # Parse command lines
    args = parse_command_line()

    imageSet = args.testSet
    testRef = "Test/%s/astc_test_reference.csv" % imageSet

    if args.refRebuild:
        run_reference_rebuild(args, imageSet, testRef)
    elif args.refUpdate:
        run_reference_rebuild(args, imageSet, testRef, True)
    else:
        run_tests(args, imageSet, testRef, args.failureDiff)


if __name__ == "__main__":
    sys.exit(main())
