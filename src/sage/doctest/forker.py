"""
Processes for running doctests

This module controls the processes started by Sage that actually run
the doctests.

EXAMPLES:

The following examples are used in doctesting this file::

    sage: doctest_var = 42; doctest_var^2
    1764
    sage: R.<a> = ZZ[]
    sage: a + doctest_var
    a + 42

AUTHORS:

- David Roe (2012-03-27) -- initial version, based on Robert Bradshaw's code.
"""

#*****************************************************************************
#       Copyright (C) 2012 David Roe <roed.math@gmail.com>
#                          Robert Bradshaw <robertwb@gmail.com>
#                          William Stein <wstein@gmail.com>
#       Copyright (C) 2013 Jeroen Demeyer <jdemeyer@cage.ugent.be>
#
#  Distributed under the terms of the GNU General Public License (GPL)
#  as published by the Free Software Foundation; either version 2 of
#  the License, or (at your option) any later version.
#                  http://www.gnu.org/licenses/
#*****************************************************************************

from __future__ import print_function

import hashlib, multiprocessing, os, sys, time, warnings, signal, linecache
import doctest, traceback
import sage.misc.randstate as randstate
from util import Timer, RecordingDict, count_noun
from sources import DictAsObject
from parsing import OriginalSource, reduce_hex
from sage.structure.sage_object import SageObject
from parsing import SageOutputChecker, pre_hash, get_source


def init_sage():
    """
    Import the Sage library.

    This function is called once at the beginning of a doctest run
    (rather than once for each file).  It imports the Sage library,
    sets DOCTEST_MODE to True, and invalidates any interfaces.

    EXAMPLES::

        sage: from sage.doctest.forker import init_sage
        sage: sage.doctest.DOCTEST_MODE = False
        sage: init_sage()
        sage: sage.doctest.DOCTEST_MODE
        True

    Check that pexpect interfaces are invalidated, but still work::

        sage: gap.eval("my_test_var := 42;")
        '42'
        sage: gap.eval("my_test_var;")
        '42'
        sage: init_sage()
        sage: gap('Group((1,2,3)(4,5), (3,4))')
        Group( [ (1,2,3)(4,5), (3,4) ] )
        sage: gap.eval("my_test_var;")
        Traceback (most recent call last):
        ...
        RuntimeError: Gap produced error output...

    Check that SymPy equation pretty printer is limited in doctest
    mode to default width (80 chars)::

        sage: from sympy import sympify
        sage: from sympy.printing.pretty.pretty import PrettyPrinter
        sage: s = sympify('+x^'.join(str(i) for i in range(30)))
        sage: print PrettyPrinter(settings={'wrap_line':True}).doprint(s)
         29    28    27    26    25    24    23    22    21    20    19    18    17
        x   + x   + x   + x   + x   + x   + x   + x   + x   + x   + x   + x   + x   +
        <BLANKLINE>
         16    15    14    13    12    11    10    9    8    7    6    5    4    3
        x   + x   + x   + x   + x   + x   + x   + x  + x  + x  + x  + x  + x  + x  + x
        <BLANKLINE>
        2
          + x
    """
    # Do this once before forking.
    import sage.all_cmdline
    sage.doctest.DOCTEST_MODE=True
    sage.interfaces.quit.invalidate_all()
    import sage.misc.displayhook
    sys.displayhook = sage.misc.displayhook.DisplayHook(sys.displayhook)

    # Switch on extra debugging
    from sage.structure.debug_options import debug
    debug.refine_category_hash_check = True

    # Disable IPython colors during doctests
    from sage.misc.interpreter import DEFAULT_SAGE_CONFIG
    DEFAULT_SAGE_CONFIG['TerminalInteractiveShell']['colors'] = 'NoColor'

    # We import readline before forking, otherwise Pdb doesn't work
    # os OS X: http://trac.sagemath.org/14289
    import readline

    # Disable SymPy terminal width detection
    from sympy.printing.pretty.stringpict import stringPict
    stringPict.terminal_width = lambda self:0


def warning_function(file):
    r"""
    Creates a function that prints warnings to the given file.

    INPUT:

    - ``file`` -- an open file handle.

    OUPUT:

    - a function that prings warnings to the given file.

    EXAMPLES::

        sage: from sage.doctest.forker import warning_function
        sage: import os
        sage: F = os.tmpfile()
        sage: wrn = warning_function(F)
        sage: wrn("bad stuff", UserWarning, "myfile.py", 0)
        sage: F.seek(0)
        sage: F.read()
        'doctest:0: UserWarning: bad stuff\n'
    """
    def doctest_showwarning(message, category, filename, lineno, file=file, line=None):
        try:
            file.write(warnings.formatwarning(message, category, 'doctest', lineno, line))
        except IOError:
            pass # the file (probably stdout) is invalid
    return doctest_showwarning


class SageSpoofInOut(SageObject):
    r"""
    We replace the standard :class:`doctest._SpoofOut` for three reasons:

    - we need to divert the output of C programs that don't print
      through sys.stdout,
    - we want the ability to recover partial output from doctest
      processes that segfault.
    - we also redirect stdin (usually from /dev/null) during doctests.

    This class defines streams ``self.real_stdin``, ``self.real_stdout``
    and ``self.real_stderr`` which refer to the original streams.

    INPUT:

    - ``outfile`` -- (default: ``os.tmpfile()``) a seekable open file
      object to which stdout and stderr should be redirected.

    - ``infile`` -- (default: ``open(os.devnull)``) an open file object
      from which stdin should be redirected.

    EXAMPLES::

        sage: import os, subprocess
        sage: from sage.doctest.forker import SageSpoofInOut
        sage: O = os.tmpfile()
        sage: S = SageSpoofInOut(O)
        sage: try:
        ....:     S.start_spoofing()
        ....:     print "hello world"
        ....: finally:
        ....:     S.stop_spoofing()
        ....:
        sage: S.getvalue()
        'hello world\n'
        sage: O.seek(0)
        sage: S = SageSpoofInOut(outfile=sys.stdout, infile=O)
        sage: try:
        ....:     S.start_spoofing()
        ....:     _ = subprocess.check_call("cat")
        ....: finally:
        ....:     S.stop_spoofing()
        ....:
        hello world
    """
    def __init__(self, outfile=None, infile=None):
        """
        Initialization.

        TESTS::

            sage: from sage.doctest.forker import SageSpoofInOut
            sage: SageSpoofInOut(os.tmpfile(), os.tmpfile())
            <class 'sage.doctest.forker.SageSpoofInOut'>
        """
        if infile is None:
            self.infile = open(os.devnull)
        else:
            self.infile = infile
        if outfile is None:
            self.outfile = os.tmpfile()
        else:
            self.outfile = outfile
        self.spoofing = False
        self.real_stdin = os.fdopen(os.dup(sys.stdin.fileno()), "r")
        self.real_stdout = os.fdopen(os.dup(sys.stdout.fileno()), "a")
        self.real_stderr = os.fdopen(os.dup(sys.stderr.fileno()), "a")
        self.position = 0

    def __del__(self):
        """
        Stop spoofing.

        TESTS::

            sage: from sage.doctest.forker import SageSpoofInOut
            sage: spoof = SageSpoofInOut()
            sage: spoof.start_spoofing()
            sage: print "Spoofed!"  # No output
            sage: del spoof
            sage: print "Not spoofed!"
            Not spoofed!
        """
        self.stop_spoofing()

    def start_spoofing(self):
        r"""
        Set stdin to read from ``self.infile`` and stdout to print to
        ``self.outfile``.

        EXAMPLES::

            sage: import os
            sage: from sage.doctest.forker import SageSpoofInOut
            sage: O = os.tmpfile()
            sage: S = SageSpoofInOut(O)
            sage: try:
            ....:     S.start_spoofing()
            ....:     print "this is not printed"
            ....: finally:
            ....:     S.stop_spoofing()
            ....:
            sage: S.getvalue()
            'this is not printed\n'
            sage: O.seek(0)
            sage: S = SageSpoofInOut(infile=O)
            sage: try:
            ....:     S.start_spoofing()
            ....:     v = sys.stdin.read()
            ....: finally:
            ....:     S.stop_spoofing()
            ....:
            sage: v
            'this is not printed\n'

        We also catch non-Python output::

            sage: try:
            ....:     S.start_spoofing()
            ....:     retval = os.system('''echo "Hello there"\nif [ $? -eq 0 ]; then\necho "good"\nfi''')
            ....: finally:
            ....:     S.stop_spoofing()
            ....:
            sage: S.getvalue()
            'Hello there\ngood\n'
        """
        if not self.spoofing:
            sys.stdout.flush()
            sys.stderr.flush()
            self.outfile.flush()
            os.dup2(self.infile.fileno(), sys.stdin.fileno())
            os.dup2(self.outfile.fileno(), sys.stdout.fileno())
            os.dup2(self.outfile.fileno(), sys.stderr.fileno())
            self.spoofing = True

    def stop_spoofing(self):
        """
        Reset stdin and stdout to their original values.

        EXAMPLES::

            sage: from sage.doctest.forker import SageSpoofInOut
            sage: S = SageSpoofInOut()
            sage: try:
            ....:     S.start_spoofing()
            ....:     print "this is not printed"
            ....: finally:
            ....:     S.stop_spoofing()
            ....:
            sage: print "this is now printed"
            this is now printed
        """
        if self.spoofing:
            sys.stdout.flush()
            sys.stderr.flush()
            self.real_stdout.flush()
            self.real_stderr.flush()
            os.dup2(self.real_stdin.fileno(), sys.stdin.fileno())
            os.dup2(self.real_stdout.fileno(), sys.stdout.fileno())
            os.dup2(self.real_stderr.fileno(), sys.stderr.fileno())
            self.spoofing = False

    def getvalue(self):
        r"""
        Gets the value that has been printed to ``outfile`` since the
        last time this function was called.

        EXAMPLES::

            sage: from sage.doctest.forker import SageSpoofInOut
            sage: S = SageSpoofInOut()
            sage: try:
            ....:     S.start_spoofing()
            ....:     print "step 1"
            ....: finally:
            ....:     S.stop_spoofing()
            ....:
            sage: S.getvalue()
            'step 1\n'
            sage: try:
            ....:     S.start_spoofing()
            ....:     print "step 2"
            ....: finally:
            ....:     S.stop_spoofing()
            ....:
            sage: S.getvalue()
            'step 2\n'
        """
        sys.stdout.flush()
        self.outfile.seek(self.position)
        result = self.outfile.read()
        self.position = self.outfile.tell()
        if not result.endswith("\n"):
            result += "\n"
        return result


class SageDocTestRunner(doctest.DocTestRunner):
    def __init__(self, *args, **kwds):
        """
        A customized version of DocTestRunner that tracks dependencies
        of doctests.

        INPUT:

        - ``stdout`` -- an open file to restore for debugging

        - ``checker`` -- None, or an instance of
          :class:`doctest.OutputChecker`

        - ``verbose`` -- boolean, determines whether verbose printing
          is enabled.

        - ``optionflags`` -- Controls the comparison with the expected
          output.  See :mod:`testmod` for more information.

        EXAMPLES::

            sage: from sage.doctest.parsing import SageOutputChecker
            sage: from sage.doctest.forker import SageDocTestRunner
            sage: from sage.doctest.control import DocTestDefaults; DD = DocTestDefaults()
            sage: import doctest, sys, os
            sage: DTR = SageDocTestRunner(SageOutputChecker(), verbose=False, sage_options=DD, optionflags=doctest.NORMALIZE_WHITESPACE|doctest.ELLIPSIS)
            sage: DTR
            <sage.doctest.forker.SageDocTestRunner instance at ...>
        """
        O = kwds.pop('outtmpfile', None)
        self.msgfile = kwds.pop('msgfile', None)
        self.options = kwds.pop('sage_options')
        doctest.DocTestRunner.__init__(self, *args, **kwds)
        self._fakeout = SageSpoofInOut(O)
        if self.msgfile is None:
            self.msgfile = self._fakeout.real_stdout
        self.history = []
        self.references = []
        self.setters = {}
        self.running_global_digest = hashlib.md5()

    def _run(self, test, compileflags, out):
        """
        This function replaces :meth:`doctest.DocTestRunner.__run`.

        It changes the following behavior:

        - We call :meth:`SageDocTestRunner.execute` rather than just
          exec

        - We don't truncate _fakeout after each example since we want
          the output file to be readable by the calling
          :class:`SageWorker`.

        Since it needs to be able to read stdout, it should be called
        while spoofing using :class:`SageSpoofInOut`.

        EXAMPLES::

            sage: from sage.doctest.parsing import SageOutputChecker
            sage: from sage.doctest.forker import SageDocTestRunner
            sage: from sage.doctest.sources import FileDocTestSource
            sage: from sage.doctest.control import DocTestDefaults; DD = DocTestDefaults()
            sage: from sage.env import SAGE_SRC
            sage: import doctest, sys, os
            sage: DTR = SageDocTestRunner(SageOutputChecker(), verbose=False, sage_options=DD, optionflags=doctest.NORMALIZE_WHITESPACE|doctest.ELLIPSIS)
            sage: filename = os.path.join(SAGE_SRC,'sage','doctest','forker.py')
            sage: FDS = FileDocTestSource(filename,DD)
            sage: doctests, extras = FDS.create_doctests(globals())
            sage: DTR.run(doctests[0], clear_globs=False) # indirect doctest
            TestResults(failed=0, attempted=4)
        """
        # Keep track of the number of failures and tries.
        failures = tries = 0

        # Save the option flags (since option directives can be used
        # to modify them).
        original_optionflags = self.optionflags

        SUCCESS, FAILURE, BOOM = range(3) # `outcome` state

        check = self._checker.check_output

        # Process each example.
        for examplenum, example in enumerate(test.examples):

            # If REPORT_ONLY_FIRST_FAILURE is set, then suppress
            # reporting after the first failure.
            quiet = (self.optionflags & doctest.REPORT_ONLY_FIRST_FAILURE and
                     failures > 0)

            # Merge in the example's options.
            self.optionflags = original_optionflags
            if example.options:
                for (optionflag, val) in example.options.items():
                    if val:
                        self.optionflags |= optionflag
                    else:
                        self.optionflags &= ~optionflag

            # If 'SKIP' is set, then skip this example.
            if self.optionflags & doctest.SKIP:
                continue

            # Record that we started this example.
            tries += 1
            # We print the example we're running for easier debugging
            # if this file times out or crashes.
            with OriginalSource(example):
                print("sage: " + example.source[:-1] + " ## line %s ##"%(test.lineno + example.lineno + 1))
            # Update the position so that result comparison works
            throwaway = self._fakeout.getvalue()
            if not quiet:
                self.report_start(out, test, example)

            # Flush files before running the example, so we know for
            # sure that everything is reported properly if the test
            # crashes.
            sys.stdout.flush()
            sys.stderr.flush()
            self.msgfile.flush()

            # Use a special filename for compile(), so we can retrieve
            # the source code during interactive debugging (see
            # __patched_linecache_getlines).
            filename = '<doctest %s[%d]>' % (test.name, examplenum)

            # Run the example in the given context (globs), and record
            # any exception that gets raised. But for SystemExit, we
            # simply propagate the exception.
            exception = None
            try:
                # Don't blink!  This is where the user's code gets run.
                compiled = compile(example.source, filename, "single",
                             compileflags, 1)
                self.execute(example, compiled, test.globs)
            except SystemExit:
                raise
            except BaseException:
                exception = sys.exc_info()
            finally:
                if self.debugger is not None:
                    self.debugger.set_continue() # ==== Example Finished ====

            got = self._fakeout.getvalue()  # the actual output
            outcome = FAILURE   # guilty until proved innocent or insane

            # If the example executed without raising any exceptions,
            # verify its output.
            if exception is None:
                if check(example.want, got, self.optionflags):
                    outcome = SUCCESS

            # The example raised an exception: check if it was expected.
            else:
                exc_info = sys.exc_info()
                exc_msg = traceback.format_exception_only(*exc_info[:2])[-1]
                if not quiet:
                    got += doctest._exception_traceback(exc_info)

                # If `example.exc_msg` is None, then we weren't expecting
                # an exception.
                if example.exc_msg is None:
                    outcome = BOOM

                # We expected an exception: see whether it matches.
                elif check(example.exc_msg, exc_msg, self.optionflags):
                    outcome = SUCCESS

                # Another chance if they didn't care about the detail.
                elif self.optionflags & doctest.IGNORE_EXCEPTION_DETAIL:
                    m1 = re.match(r'(?:[^:]*\.)?([^:]*:)', example.exc_msg)
                    m2 = re.match(r'(?:[^:]*\.)?([^:]*:)', exc_msg)
                    if m1 and m2 and check(m1.group(1), m2.group(1),
                                           self.optionflags):
                        outcome = SUCCESS

            # Report the outcome.
            if outcome is SUCCESS:
                if self.options.warn_long and example.walltime > self.options.warn_long:
                    self.report_overtime(out, test, example, got)
                    failures += 1
                elif not quiet:
                    self.report_success(out, test, example, got)
            elif outcome is FAILURE:
                if not quiet:
                    self.report_failure(out, test, example, got, test.globs)
                failures += 1
            elif outcome is BOOM:
                if not quiet:
                    self.report_unexpected_exception(out, test, example,
                                                     exc_info)
                failures += 1
            else:
                assert False, ("unknown outcome", outcome)

        # Restore the option flags (in case they were modified)
        self.optionflags = original_optionflags

        # Record and return the number of failures and tries.
        self._DocTestRunner__record_outcome(test, failures, tries)
        return doctest.TestResults(failures, tries)

    def run(self, test, compileflags=None, out=None, clear_globs=True):
        """
        Runs the examples in a given doctest.

        This function replaces :class:`doctest.DocTestRunner.run`
        since it needs to handle spoofing. It also leaves the display
        hook in place.

        INPUT:

        - ``test`` -- an instance of :class:`doctest.DocTest`

        - ``compileflags`` -- the set of compiler flags used to
          execute examples (passed in to the :func:`compile`).  If
          None, they are filled in from the result of
          :func:`doctest._extract_future_flags` applied to
          ``test.globs``.

        - ``out`` -- a function for writing the output (defaults to
          :func:`sys.stdout.write`).

        - ``clear_globs`` -- boolean (default True): whether to clear
          the namespace after running this doctest.

        OUTPUT:

        - ``f`` -- integer, the number of examples that failed

        - ``t`` -- the number of examples tried

        EXAMPLES::

            sage: from sage.doctest.parsing import SageOutputChecker
            sage: from sage.doctest.forker import SageDocTestRunner
            sage: from sage.doctest.sources import FileDocTestSource
            sage: from sage.doctest.control import DocTestDefaults; DD = DocTestDefaults()
            sage: from sage.env import SAGE_SRC
            sage: import doctest, sys, os
            sage: DTR = SageDocTestRunner(SageOutputChecker(), verbose=False, sage_options=DD, optionflags=doctest.NORMALIZE_WHITESPACE|doctest.ELLIPSIS)
            sage: filename = os.path.join(SAGE_SRC,'sage','doctest','forker.py')
            sage: FDS = FileDocTestSource(filename,DD)
            sage: doctests, extras = FDS.create_doctests(globals())
            sage: DTR.run(doctests[0], clear_globs=False)
            TestResults(failed=0, attempted=4)
        """
        self.setters = {}
        randstate.set_random_seed(long(0))
        warnings.showwarning = warning_function(sys.stdout)
        self.running_doctest_digest = hashlib.md5()
        self.test = test
        if compileflags is None:
            compileflags = doctest._extract_future_flags(test.globs)
        # We use this slightly modified version of Pdb because it
        # interacts better with the doctesting framework (like allowing
        # doctests for sys.settrace()). Since we already have output
        # spoofing in place, there is no need for redirection.
        if self.options.debug:
            self.debugger = doctest._OutputRedirectingPdb(sys.stdout)
            self.debugger.reset()
        else:
            self.debugger = None
        self.save_linecache_getlines = linecache.getlines
        linecache.getlines = self._DocTestRunner__patched_linecache_getlines
        if out is None:
            def out(s):
                self.msgfile.write(s)

        self._fakeout.start_spoofing()
        # If self.options.initial is set, we show only the first failure in each doctest block.
        self.no_failure_yet = True
        try:
            return self._run(test, compileflags, out)
        finally:
            self._fakeout.stop_spoofing()
            linecache.getlines = self.save_linecache_getlines
            if clear_globs:
                test.globs.clear()

    def summarize(self, verbose=None):
        """
        Print results of testing to ``self.msgfile`` and return number
        of failures and tests run.

        INPUT:

        - ``verbose`` -- whether to print lots of stuff

        OUTPUT:

        - returns ``(f, t)``, a :class:`doctest.TestResults` instance
          giving the number of failures and the total number of tests
          run.

        EXAMPLES::

            sage: from sage.doctest.parsing import SageOutputChecker
            sage: from sage.doctest.forker import SageDocTestRunner
            sage: from sage.doctest.control import DocTestDefaults; DD = DocTestDefaults()
            sage: import doctest, sys, os
            sage: DTR = SageDocTestRunner(SageOutputChecker(), verbose=False, sage_options=DD, optionflags=doctest.NORMALIZE_WHITESPACE|doctest.ELLIPSIS)
            sage: DTR._name2ft['sage.doctest.forker'] = (1,120)
            sage: results = DTR.summarize()
            **********************************************************************
            1 item had failures:
                1 of 120 in sage.doctest.forker
            sage: results
            TestResults(failed=1, attempted=120)
        """
        if verbose is None:
            verbose = self._verbose
        m = self.msgfile
        notests = []
        passed = []
        failed = []
        totalt = totalf = 0
        for x in self._name2ft.items():
            name, (f, t) = x
            assert f <= t
            totalt += t
            totalf += f
            if not t:
                notests.append(name)
            elif not f:
                passed.append( (name, t) )
            else:
                failed.append(x)
        if verbose:
            if notests:
                print(count_noun(len(notests), "item"), "had no tests:", file=m)
                notests.sort()
                for thing in notests:
                    print("    %s"%thing, file=m)
            if passed:
                print(count_noun(len(passed), "item"), "passed all tests:", file=m)
                passed.sort()
                for thing, count in passed:
                    print(" %s in %s"%(count_noun(count, "test", pad_number=3, pad_noun=True), thing), file=m)
        if failed:
            print(self.DIVIDER, file=m)
            print(count_noun(len(failed), "item"), "had failures:", file=m)
            failed.sort()
            for thing, (f, t) in failed:
                print(" %3d of %3d in %s"%(f, t, thing), file=m)
        if verbose:
            print(count_noun(totalt, "test") + " in " + count_noun(len(self._name2ft), "item") + ".", file=m)
            print("%s passed and %s failed."%(totalt - totalf, totalf), file=m)
            if totalf:
                print("***Test Failed***", file=m)
            else:
                print("Test passed.", file=m)
        m.flush()
        return doctest.TestResults(totalf, totalt)

    def update_digests(self, example):
        """
        Update global and doctest digests.

        Sage's doctest runner tracks the state of doctests so that
        their dependencies are known.  For example, in the following
        two lines ::

            sage: R.<x> = ZZ[]
            sage: f = x^2 + 1

        it records that the second line depends on the first since the
        first INSERTS ``x`` into the global namespace and the second
        line RETRIEVES ``x`` from the global namespace.

        This function updates the hashes that record these
        dependencies.

        INPUT:

        - ``example`` -- a :class:`doctest.Example` instance

        EXAMPLES::

            sage: from sage.doctest.parsing import SageOutputChecker
            sage: from sage.doctest.forker import SageDocTestRunner
            sage: from sage.doctest.sources import FileDocTestSource
            sage: from sage.doctest.control import DocTestDefaults; DD = DocTestDefaults()
            sage: from sage.env import SAGE_SRC
            sage: import doctest, sys, os, hashlib
            sage: DTR = SageDocTestRunner(SageOutputChecker(), verbose=False, sage_options=DD, optionflags=doctest.NORMALIZE_WHITESPACE|doctest.ELLIPSIS)
            sage: filename = os.path.join(SAGE_SRC,'sage','doctest','forker.py')
            sage: FDS = FileDocTestSource(filename,DD)
            sage: doctests, extras = FDS.create_doctests(globals())
            sage: DTR.running_global_digest.hexdigest()
            'd41d8cd98f00b204e9800998ecf8427e'
            sage: DTR.running_doctest_digest = hashlib.md5()
            sage: ex = doctests[0].examples[0]; ex.predecessors = None
            sage: DTR.update_digests(ex)
            sage: DTR.running_global_digest.hexdigest()
            '3cb44104292c3a3ab4da3112ce5dc35c'
        """
        s = pre_hash(get_source(example))
        self.running_global_digest.update(s)
        self.running_doctest_digest.update(s)
        if example.predecessors is not None:
            digest = hashlib.md5(s)
            digest.update(reduce_hex(e.running_state for e in example.predecessors))
            example.running_state = digest.hexdigest()

    def execute(self, example, compiled, globs):
        """
        Runs the given example, recording dependencies.

        Rather than using a basic dictionary, Sage's doctest runner
        uses a :class:`sage.doctest.util.RecordingDict`, which records
        every time a value is set or retrieved.  Executing the given
        code with this recording dictionary as the namespace allows
        Sage to track dependencies between doctest lines.  For
        example, in the following two lines ::

            sage: R.<x> = ZZ[]
            sage: f = x^2 + 1

        the recording dictionary records that the second line depends
        on the first since the first INSERTS ``x`` into the global
        namespace and the second line RETRIEVES ``x`` from the global
        namespace.

        INPUT:

        - ``example`` -- a :class:`doctest.Example` instance.
        - ``compiled`` -- a code object produced by compiling ``example.source``
        - ``globs`` -- a dictionary in which to execute ``compiled``.

        OUTPUT:

        - the output of the compiled code snippet.

        EXAMPLES::

            sage: from sage.doctest.parsing import SageOutputChecker
            sage: from sage.doctest.forker import SageDocTestRunner
            sage: from sage.doctest.sources import FileDocTestSource
            sage: from sage.doctest.util import RecordingDict
            sage: from sage.doctest.control import DocTestDefaults; DD = DocTestDefaults()
            sage: from sage.env import SAGE_SRC
            sage: import doctest, sys, os, hashlib
            sage: DTR = SageDocTestRunner(SageOutputChecker(), verbose=False, sage_options=DD, optionflags=doctest.NORMALIZE_WHITESPACE|doctest.ELLIPSIS)
            sage: DTR.running_doctest_digest = hashlib.md5()
            sage: filename = os.path.join(SAGE_SRC,'sage','doctest','forker.py')
            sage: FDS = FileDocTestSource(filename,DD)
            sage: globs = RecordingDict(globals())
            sage: globs.has_key('doctest_var')
            False
            sage: doctests, extras = FDS.create_doctests(globs)
            sage: ex0 = doctests[0].examples[0]
            sage: compiled = compile(ex0.source, '<doctest sage.doctest.forker[0]>', 'single', 32768, 1)
            sage: DTR.execute(ex0, compiled, globs)
            1764
            sage: globs['doctest_var']
            42
            sage: globs.set
            set(['doctest_var'])
            sage: globs.got
            set(['Integer'])

        Now we can execute some more doctests to see the dependencies. ::

            sage: ex1 = doctests[0].examples[1]
            sage: compiled = compile(ex1.source, '<doctest sage.doctest.forker[1]>', 'single', 32768, 1)
            sage: DTR.execute(ex1, compiled, globs)
            sage: sorted(list(globs.set))
            ['R', 'a']
            sage: globs.got
            set(['ZZ'])
            sage: ex1.predecessors
            []

        ::

            sage: ex2 = doctests[0].examples[2]
            sage: compiled = compile(ex2.source, '<doctest sage.doctest.forker[2]>', 'single', 32768, 1)
            sage: DTR.execute(ex2, compiled, globs)
            a + 42
            sage: list(globs.set)
            []
            sage: sorted(list(globs.got))
            ['a', 'doctest_var']
            sage: set(ex2.predecessors) == set([ex0,ex1])
            True
        """
        if isinstance(globs, RecordingDict):
            globs.start()
        example.sequence_number = len(self.history)
        self.history.append(example)
        timer = Timer().start()
        try:
            exec compiled in globs
        finally:
            timer.stop().annotate(example)
            if isinstance(globs, RecordingDict):
                example.predecessors = []
                for name in globs.got:
                    ref = self.setters.get(name)
                    if ref is not None:
                        example.predecessors.append(ref)
                for name in globs.set:
                    self.setters[name] = example
            else:
                example.predecessors = None
            self.update_digests(example)
            example.total_state = self.running_global_digest.hexdigest()
            example.doctest_state = self.running_doctest_digest.hexdigest()

    def _failure_header(self, test, example):
        """
        We strip out ``sage:`` prompts, so we override
        :meth:`doctest.DocTestRunner._failure_header` for better
        reporting.

        INPUT:

        - ``test`` -- a :class:`doctest.DocTest` instance

        - ``example`` -- a :class:`doctest.Example` instance in ``test``.

        OUTPUT:

        - a string used for reporting that the given example failed.

        EXAMPLES::

            sage: from sage.doctest.parsing import SageOutputChecker
            sage: from sage.doctest.forker import SageDocTestRunner
            sage: from sage.doctest.sources import FileDocTestSource
            sage: from sage.doctest.control import DocTestDefaults; DD = DocTestDefaults()
            sage: from sage.env import SAGE_SRC
            sage: import doctest, sys, os
            sage: DTR = SageDocTestRunner(SageOutputChecker(), verbose=False, sage_options=DD, optionflags=doctest.NORMALIZE_WHITESPACE|doctest.ELLIPSIS)
            sage: filename = os.path.join(SAGE_SRC,'sage','doctest','forker.py')
            sage: FDS = FileDocTestSource(filename,DD)
            sage: doctests, extras = FDS.create_doctests(globals())
            sage: ex = doctests[0].examples[0]
            sage: print DTR._failure_header(doctests[0], ex)
            **********************************************************************
            File ".../sage/doctest/forker.py", line 11, in sage.doctest.forker
            Failed example:
                doctest_var = 42; doctest_var^2
            <BLANKLINE>

       Without the source swapping::

            sage: import doctest
            sage: print doctest.DocTestRunner._failure_header(DTR, doctests[0], ex)
            **********************************************************************
            File ".../sage/doctest/forker.py", line 11, in sage.doctest.forker
            Failed example:
                doctest_var = Integer(42); doctest_var**Integer(2)
            <BLANKLINE>
        """
        with OriginalSource(example):
            return doctest.DocTestRunner._failure_header(self, test, example)

    def report_start(self, out, test, example):
        """
        Called when an example starts.

        INPUT:

        - ``out`` -- a function for printing

        - ``test`` -- a :class:`doctest.DocTest` instance

        - ``example`` -- a :class:`doctest.Example` instance in ``test``

        OUTPUT:

        - prints a report to ``out``

        EXAMPLES::

            sage: from sage.doctest.parsing import SageOutputChecker
            sage: from sage.doctest.forker import SageDocTestRunner
            sage: from sage.doctest.sources import FileDocTestSource
            sage: from sage.doctest.control import DocTestDefaults; DD = DocTestDefaults()
            sage: from sage.env import SAGE_SRC
            sage: import doctest, sys, os
            sage: DTR = SageDocTestRunner(SageOutputChecker(), verbose=True, sage_options=DD, optionflags=doctest.NORMALIZE_WHITESPACE|doctest.ELLIPSIS)
            sage: filename = os.path.join(SAGE_SRC,'sage','doctest','forker.py')
            sage: FDS = FileDocTestSource(filename,DD)
            sage: doctests, extras = FDS.create_doctests(globals())
            sage: ex = doctests[0].examples[0]
            sage: DTR.report_start(sys.stdout.write, doctests[0], ex)
            Trying (line 11):    doctest_var = 42; doctest_var^2
            Expecting:
                1764
        """
        # We completely replace doctest.DocTestRunner.report_start so that we can include line numbers
        with OriginalSource(example):
            if self._verbose:
                start_txt = ('Trying (line %s):'%(test.lineno + example.lineno + 1)
                             + doctest._indent(example.source))
                if example.want:
                    start_txt += 'Expecting:\n' + doctest._indent(example.want)
                else:
                    start_txt += 'Expecting nothing\n'
                out(start_txt)

    def report_success(self, out, test, example, got):
        """
        Called when an example succeeds.

        INPUT:

        - ``out`` -- a function for printing

        - ``test`` -- a :class:`doctest.DocTest` instance

        - ``example`` -- a :class:`doctest.Example` instance in ``test``

        - ``got`` -- a string, the result of running ``example``

        OUTPUT:

        - prints a report to ``out``

        - if in debugging mode, starts an IPython prompt at the point
          of the failure

        EXAMPLES::

            sage: from sage.doctest.parsing import SageOutputChecker
            sage: from sage.doctest.forker import SageDocTestRunner
            sage: from sage.doctest.sources import FileDocTestSource
            sage: from sage.doctest.control import DocTestDefaults; DD = DocTestDefaults()
            sage: from sage.misc.misc import walltime
            sage: from sage.env import SAGE_SRC
            sage: import doctest, sys, os
            sage: DTR = SageDocTestRunner(SageOutputChecker(), verbose=True, sage_options=DD, optionflags=doctest.NORMALIZE_WHITESPACE|doctest.ELLIPSIS)
            sage: filename = os.path.join(SAGE_SRC,'sage','doctest','forker.py')
            sage: FDS = FileDocTestSource(filename,DD)
            sage: doctests, extras = FDS.create_doctests(globals())
            sage: ex = doctests[0].examples[0]
            sage: ex.walltime = 0.0
            sage: DTR.report_success(sys.stdout.write, doctests[0], ex, '1764')
            ok [0.00 s]
        """
        # We completely replace doctest.DocTestRunner.report_success so that we can include time taken for the test
        if self._verbose:
            out("ok [%.2f s]\n"%example.walltime)

    def report_failure(self, out, test, example, got, globs):
        r"""
        Called when a doctest fails.

        INPUT:

        - ``out`` -- a function for printing

        - ``test`` -- a :class:`doctest.DocTest` instance

        - ``example`` -- a :class:`doctest.Example` instance in ``test``

        - ``got`` -- a string, the result of running ``example``

        - ``globs`` -- a dictionary of globals, used if in debugging mode

        OUTPUT:

        - prints a report to ``out``

        EXAMPLES::

            sage: from sage.doctest.parsing import SageOutputChecker
            sage: from sage.doctest.forker import SageDocTestRunner
            sage: from sage.doctest.sources import FileDocTestSource
            sage: from sage.doctest.control import DocTestDefaults; DD = DocTestDefaults()
            sage: from sage.env import SAGE_SRC
            sage: import doctest, sys, os
            sage: DTR = SageDocTestRunner(SageOutputChecker(), verbose=True, sage_options=DD, optionflags=doctest.NORMALIZE_WHITESPACE|doctest.ELLIPSIS)
            sage: filename = os.path.join(SAGE_SRC,'sage','doctest','forker.py')
            sage: FDS = FileDocTestSource(filename,DD)
            sage: doctests, extras = FDS.create_doctests(globals())
            sage: ex = doctests[0].examples[0]
            sage: DTR.no_failure_yet = True
            sage: DTR.report_failure(sys.stdout.write, doctests[0], ex, 'BAD ANSWER\n', {})
            **********************************************************************
            File ".../sage/doctest/forker.py", line 11, in sage.doctest.forker
            Failed example:
                doctest_var = 42; doctest_var^2
            Expected:
                1764
            Got:
                BAD ANSWER

        If debugging is turned on this function starts an IPython
        prompt when a test returns an incorrect answer::

            sage: import os
            sage: os.environ['SAGE_PEXPECT_LOG'] = "1"
            sage: sage0.quit()
            sage: _ = sage0.eval("import doctest, sys, os, multiprocessing, subprocess")
            sage: _ = sage0.eval("from sage.doctest.parsing import SageOutputChecker")
            sage: _ = sage0.eval("import sage.doctest.forker as sdf")
            sage: _ = sage0.eval("sdf.init_sage()")
            sage: _ = sage0.eval("from sage.doctest.control import DocTestDefaults")
            sage: _ = sage0.eval("DD = DocTestDefaults(debug=True)")
            sage: _ = sage0.eval("ex1 = doctest.Example('a = 17', '')")
            sage: _ = sage0.eval("ex2 = doctest.Example('2*a', '1')")
            sage: _ = sage0.eval("DT = doctest.DocTest([ex1,ex2], globals(), 'doubling', None, 0, None)")
            sage: _ = sage0.eval("DTR = sdf.SageDocTestRunner(SageOutputChecker(), verbose=False, sage_options=DD, optionflags=doctest.NORMALIZE_WHITESPACE|doctest.ELLIPSIS)")
            sage: sage0._prompt = r"debug: "
            sage: print sage0.eval("DTR.run(DT, clear_globs=False)") # indirect doctest
            **********************************************************************
            Line 1, in doubling
            Failed example:
                2*a
            Expected:
                1
            Got:
                34
            **********************************************************************
            Previously executed commands:
            ...
            sage: sage0.eval("a")
            '...17'
            sage: sage0._prompt = "sage: "
            sage: sage0.eval("quit")
            'Returning to doctests...TestResults(failed=1, attempted=2)'
        """
        if not self.options.initial or self.no_failure_yet:
            self.no_failure_yet = False
            returnval = doctest.DocTestRunner.report_failure(self, out, test, example, got)
            if self.options.debug:
                self._fakeout.stop_spoofing()
                restore_tcpgrp = None
                try:
                    if os.isatty(0):
                        # In order to read from the terminal, we need
                        # to make the current process group the
                        # foreground group.
                        restore_tcpgrp = os.tcgetpgrp(0)
                        signal.signal(signal.SIGTTIN, signal.SIG_IGN)
                        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
                        os.tcsetpgrp(0, os.getpgrp())
                    print("*" * 70)
                    print("Previously executed commands:")
                    for ex in test.examples:
                        if ex is example:
                            break
                        if hasattr(ex, 'sage_source'):
                            src = '    sage: ' + ex.sage_source
                        else:
                            src = '    sage: ' + ex.source
                        if src[-1] == '\n':
                            src = src[:-1]
                        src = src.replace('\n', '\n    ....: ')
                        print(src)
                        if ex.want:
                            print(doctest._indent(ex.want[:-1]))
                    from sage.misc.interpreter import DEFAULT_SAGE_CONFIG
                    from IPython import embed
                    cfg = DEFAULT_SAGE_CONFIG.copy()
                    prompt_config = cfg.PromptManager
                    prompt_config.in_template = 'debug: '
                    prompt_config.in2_template = '.....: '
                    embed(config=cfg, banner1='', user_ns=dict(globs))
                except KeyboardInterrupt:
                    # Assume this is a *real* interrupt. We need to
                    # escalate this to the master docbuilding process.
                    if not self.options.serial:
                        os.kill(os.getppid(), signal.SIGINT)
                    raise
                finally:
                    # Restore the foreground process group.
                    if restore_tcpgrp is not None:
                        os.tcsetpgrp(0, restore_tcpgrp)
                        signal.signal(signal.SIGTTIN, signal.SIG_DFL)
                        signal.signal(signal.SIGTTOU, signal.SIG_DFL)
                    print("Returning to doctests...")
                    self._fakeout.start_spoofing()
            return returnval

    def report_overtime(self, out, test, example, got):
        r"""
        Called when the ``warn_long`` option flag is set and a doctest
        runs longer than the specified time.

        INPUT:

        - ``out`` -- a function for printing

        - ``test`` -- a :class:`doctest.DocTest` instance

        - ``example`` -- a :class:`doctest.Example` instance in ``test``

        - ``got`` -- a string, the result of running ``example``

        OUTPUT:

        - prints a report to ``out``

        EXAMPLES::

            sage: from sage.doctest.parsing import SageOutputChecker
            sage: from sage.doctest.forker import SageDocTestRunner
            sage: from sage.doctest.sources import FileDocTestSource
            sage: from sage.doctest.control import DocTestDefaults; DD = DocTestDefaults()
            sage: from sage.misc.misc import walltime
            sage: from sage.env import SAGE_SRC
            sage: import doctest, sys, os
            sage: DTR = SageDocTestRunner(SageOutputChecker(), verbose=True, sage_options=DD, optionflags=doctest.NORMALIZE_WHITESPACE|doctest.ELLIPSIS)
            sage: filename = os.path.join(SAGE_SRC,'sage','doctest','forker.py')
            sage: FDS = FileDocTestSource(filename,DD)
            sage: doctests, extras = FDS.create_doctests(globals())
            sage: ex = doctests[0].examples[0]
            sage: ex.walltime = 1.23
            sage: DTR.report_overtime(sys.stdout.write, doctests[0], ex, 'BAD ANSWER\n')
            **********************************************************************
            File ".../sage/doctest/forker.py", line 11, in sage.doctest.forker
            Failed example:
                doctest_var = 42; doctest_var^2
            Test ran for 1.23 s
        """
        out(self._failure_header(test, example) +
            "Test ran for %.2f s\n"%example.walltime)

    def report_unexpected_exception(self, out, test, example, exc_info):
        r"""
        Called when a doctest raises an exception that's not matched by the expected output.

        If debugging has been turned on, starts an interactive debugger.

        INPUT:

        - ``out`` -- a function for printing

        - ``test`` -- a :class:`doctest.DocTest` instance

        - ``example`` -- a :class:`doctest.Example` instance in ``test``

        - ``exc_info`` -- the result of ``sys.exc_info()``

        OUTPUT:

        - prints a report to ``out``

        - if in debugging mode, starts PDB with the given traceback

        EXAMPLES::

            sage: import os
            sage: os.environ['SAGE_PEXPECT_LOG'] = "1"
            sage: sage0.quit()
            sage: _ = sage0.eval("import doctest, sys, os, multiprocessing, subprocess")
            sage: _ = sage0.eval("from sage.doctest.parsing import SageOutputChecker")
            sage: _ = sage0.eval("import sage.doctest.forker as sdf")
            sage: _ = sage0.eval("from sage.doctest.control import DocTestDefaults")
            sage: _ = sage0.eval("DD = DocTestDefaults(debug=True)")
            sage: _ = sage0.eval("ex = doctest.Example('E = EllipticCurve([0,0]); E', 'A singular Elliptic Curve')")
            sage: _ = sage0.eval("DT = doctest.DocTest([ex], globals(), 'singular_curve', None, 0, None)")
            sage: _ = sage0.eval("DTR = sdf.SageDocTestRunner(SageOutputChecker(), verbose=False, sage_options=DD, optionflags=doctest.NORMALIZE_WHITESPACE|doctest.ELLIPSIS)")
            sage: sage0._prompt = r"\(Pdb\) "
            sage: sage0.eval("DTR.run(DT, clear_globs=False)") # indirect doctest
            '... "Invariants %s define a singular curve."%ainvs'
            sage: sage0.eval("l")
            '...if self.discriminant() == 0:...raise ArithmeticError...'
            sage: sage0.eval("u")
            '...EllipticCurve_field.__init__(self, [field(x) for x in ainvs])'
            sage: sage0.eval("p ainvs")
            '[0, 0]'
            sage: sage0._prompt = "sage: "
            sage: sage0.eval("quit")
            'TestResults(failed=1, attempted=1)'
        """
        if not self.options.initial or self.no_failure_yet:
            self.no_failure_yet = False
            returnval = doctest.DocTestRunner.report_unexpected_exception(self, out, test, example, exc_info)
            if self.options.debug:
                self._fakeout.stop_spoofing()
                restore_tcpgrp = None
                try:
                    if os.isatty(0):
                        # In order to read from the terminal, we need
                        # to make the current process group the
                        # foreground group.
                        restore_tcpgrp = os.tcgetpgrp(0)
                        signal.signal(signal.SIGTTIN, signal.SIG_IGN)
                        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
                        os.tcsetpgrp(0, os.getpgrp())

                    exc_type, exc_val, exc_tb = exc_info
                    self.debugger.reset()
                    self.debugger.interaction(None, exc_tb)
                except KeyboardInterrupt:
                    # Assume this is a *real* interrupt. We need to
                    # escalate this to the master docbuilding process.
                    if not self.options.serial:
                        os.kill(os.getppid(), signal.SIGINT)
                    raise
                finally:
                    # Restore the foreground process group.
                    if restore_tcpgrp is not None:
                        os.tcsetpgrp(0, restore_tcpgrp)
                        signal.signal(signal.SIGTTIN, signal.SIG_DFL)
                        signal.signal(signal.SIGTTOU, signal.SIG_DFL)
                    self._fakeout.start_spoofing()
            return returnval

    def update_results(self, D):
        """
        When returning results we pick out the results of interest
        since many attributes are not pickleable.

        INPUT:

        - ``D`` -- a dictionary to update with cputime and walltime

        OUTPUT:

        - the number of failures (or False if there is no failure attribute)

        EXAMPLES::

            sage: from sage.doctest.parsing import SageOutputChecker
            sage: from sage.doctest.forker import SageDocTestRunner
            sage: from sage.doctest.sources import FileDocTestSource, DictAsObject
            sage: from sage.doctest.control import DocTestDefaults; DD = DocTestDefaults()
            sage: from sage.env import SAGE_SRC
            sage: import doctest, sys, os
            sage: DTR = SageDocTestRunner(SageOutputChecker(), verbose=False, sage_options=DD, optionflags=doctest.NORMALIZE_WHITESPACE|doctest.ELLIPSIS)
            sage: filename = os.path.join(SAGE_SRC,'sage','doctest','forker.py')
            sage: FDS = FileDocTestSource(filename,DD)
            sage: doctests, extras = FDS.create_doctests(globals())
            sage: from sage.doctest.util import Timer
            sage: T = Timer().start()
            sage: DTR.run(doctests[0])
            TestResults(failed=0, attempted=4)
            sage: T.stop().annotate(DTR)
            sage: D = DictAsObject({'cputime':[],'walltime':[],'err':None})
            sage: DTR.update_results(D)
            0
            sage: sorted(list(D.iteritems()))
            [('cputime', [...]), ('err', None), ('failures', 0), ('walltime', [...])]
        """
        for key in ["cputime","walltime"]:
            if not D.has_key(key):
                D[key] = []
            if hasattr(self, key):
                D[key].append(self.__dict__[key])
        if hasattr(self, 'failures'):
            D['failures'] = self.failures
            return self.failures
        else:
            return False

def dummy_handler(sig, frame):
    """
    Dummy signal handler for SIGCHLD (just to ensure the signal
    isn't ignored).

    TESTS::

        sage: import signal
        sage: from sage.doctest.forker import dummy_handler
        sage: _ = signal.signal(signal.SIGUSR1, dummy_handler)
        sage: os.kill(os.getpid(), signal.SIGUSR1)
        sage: signal.signal(signal.SIGUSR1, signal.SIG_DFL)
        <function dummy_handler at ...>
    """
    pass

class DocTestDispatcher(SageObject):
    """
    Creates parallel :class:`DocTestWorker` processes and dispatches
    doctesting tasks.
    """
    def __init__(self, controller):
        """
        INPUT:

        - ``controller`` -- a :class:`sage.doctest.control.DocTestController` instance

        EXAMPLES::

            sage: from sage.doctest.control import DocTestController, DocTestDefaults
            sage: from sage.doctest.forker import DocTestDispatcher
            sage: DocTestDispatcher(DocTestController(DocTestDefaults(), []))
            <class 'sage.doctest.forker.DocTestDispatcher'>
        """
        self.controller = controller
        init_sage()

    def serial_dispatch(self):
        """
        Run the doctests from the controller's specified sources in series.

        There is no graceful handling for signals, no possibility of
        interrupting tests and no timeout.

        EXAMPLES::

            sage: from sage.doctest.control import DocTestController, DocTestDefaults
            sage: from sage.doctest.forker import DocTestDispatcher
            sage: from sage.doctest.reporting import DocTestReporter
            sage: from sage.doctest.util import Timer
            sage: from sage.env import SAGE_SRC
            sage: import os
            sage: homset = os.path.join(SAGE_SRC, 'sage', 'rings', 'homset.py')
            sage: ideal = os.path.join(SAGE_SRC, 'sage', 'rings', 'ideal.py')
            sage: DC = DocTestController(DocTestDefaults(), [homset, ideal])
            sage: DC.expand_files_into_sources()
            sage: DD = DocTestDispatcher(DC)
            sage: DR = DocTestReporter(DC)
            sage: DC.reporter = DR
            sage: DC.dispatcher = DD
            sage: DC.timer = Timer().start()
            sage: DD.serial_dispatch()
            sage -t .../rings/homset.py
                [... tests, ... s]
            sage -t .../rings/ideal.py
                [... tests, ... s]
        """
        for source in self.controller.sources:
            head = self.controller.reporter.report_head(source)
            self.controller.log(head)
            outtmpfile = os.tmpfile()
            result = DocTestTask(source)(self.controller.options, outtmpfile)
            outtmpfile.seek(0)
            output = outtmpfile.read()
            self.controller.reporter.report(source, False, 0, result, output)

    def parallel_dispatch(self):
        """
        Run the doctests from the controller's specified sources in parallel.

        This creates :class:`DocTestWorker` subprocesses, while the master
        process checks for timeouts and collects and displays the results.

        EXAMPLES::

            sage: from sage.doctest.control import DocTestController, DocTestDefaults
            sage: from sage.doctest.forker import DocTestDispatcher
            sage: from sage.doctest.reporting import DocTestReporter
            sage: from sage.doctest.util import Timer
            sage: from sage.env import SAGE_SRC
            sage: import os
            sage: crem = os.path.join(SAGE_SRC, 'sage', 'databases', 'cremona.py')
            sage: bigo = os.path.join(SAGE_SRC, 'sage', 'rings', 'big_oh.py')
            sage: DC = DocTestController(DocTestDefaults(), [crem, bigo])
            sage: DC.expand_files_into_sources()
            sage: DD = DocTestDispatcher(DC)
            sage: DR = DocTestReporter(DC)
            sage: DC.reporter = DR
            sage: DC.dispatcher = DD
            sage: DC.timer = Timer().start()
            sage: DD.parallel_dispatch()
            sage -t .../databases/cremona.py
                [... tests, ... s]
            sage -t .../rings/big_oh.py
                [... tests, ... s]
        """
        opt = self.controller.options
        source_iter = iter(self.controller.sources)

        # If timeout was 0, simply set a very long time
        if opt.timeout <= 0:
            opt.timeout = 2**60
        # Timeout we give a process to die (after it received a SIGHUP
        # signal). If it doesn't exit by itself in this many seconds, we
        # SIGKILL it. This is 5% of doctest timeout, with a maximum of
        # 10 minutes and a minimum of 20 seconds.
        die_timeout = opt.timeout * 0.05
        if die_timeout > 600:
            die_timeout = 600
        elif die_timeout < 20:
            die_timeout = 20

        # List of alive DocTestWorkers (child processes). Workers which
        # are done but whose messages have not been read are also
        # considered alive.
        workers = []

        # List of DocTestWorkers which have finished running but
        # whose results have not been reported yet.
        finished = []

        # One particular worker that we are "following": we report the
        # messages while it's running. For other workers, we report the
        # messages if there is no followed worker.
        follow = None

        # Install signal handler for SIGCHLD
        signal.signal(signal.SIGCHLD, dummy_handler)

        # Logger
        log = self.controller.log

        from sage.ext.pselect import PSelecter
        try:
            # Block SIGCHLD and SIGINT except during the pselect() call
            with PSelecter([signal.SIGCHLD, signal.SIGINT]) as sel:
                # Function to execute in the child process which exits
                # this "with" statement (which restores the signal mask)
                # and resets to SIGCHLD handler to default.
                # Since multiprocessing.Process is implemented using
                # fork(), signals would otherwise remain blocked in the
                # child process.
                def sel_exit():
                    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                    sel.__exit__(None, None, None)

                while True:
                    # To avoid calling time.time() all the time while
                    # checking for timeouts, we call it here, once per
                    # loop. It's not a problem if this isn't very
                    # precise, doctest timeouts don't need millisecond
                    # precision.
                    now = time.time()

                    # If there were any substantial changes in the state
                    # (new worker started or finished worker reported),
                    # restart this while loop instead of calling pselect().
                    # This ensures internal consistency and a reasonably
                    # accurate value for "now".
                    restart = False

                    # Process all workers. Check for timeouts on active
                    # workers and move finished/crashed workers to the
                    # "finished" list.
                    # Create a new list "new_workers" containing the active
                    # workers (to avoid updating "workers" in place).
                    new_workers = []
                    for w in workers:
                        if w.rmessages is not None or w.is_alive():
                            if now >= w.deadline:
                                # Timeout => (try to) kill the process
                                # group (which normally includes
                                # grandchildren) and close the message
                                # pipe.
                                # We don't report the timeout yet, we wait
                                # until the process has actually died.
                                w.kill()
                                w.deadline = now + die_timeout
                            if not w.is_alive():
                                # Worker is done but we haven't read all
                                # messages (possibly a grandchild still
                                # has the messages pipe open).
                                # Adjust deadline to read all messages:
                                newdeadline = now + die_timeout
                                if w.deadline > newdeadline:
                                    w.deadline = newdeadline
                            new_workers.append(w)
                        else:
                            # Save the result and output of the worker
                            # and close the associated file descriptors.
                            # It is important to do this now. If we
                            # would leave them open until we call
                            # report(), parallel testing can easily fail
                            # with a "Too many open files" error.
                            w.save_result_output()
                            finished.append(w)
                    workers = new_workers

                    # Similarly, process finished workers.
                    new_finished = []
                    for w in finished:
                        if follow is not None and follow is not w:
                            # We are following a different worker, so
                            # we cannot report now
                            new_finished.append(w)
                        else:
                            # Report the completion of this worker
                            if w.heading is not None:
                                log(w.heading)
                            log(w.messages, end="")
                            self.controller.reporter.report(
                                w.source,
                                w.killed,
                                w.exitcode,
                                w.result,
                                w.output,
                                pid=w.pid)
                            restart = True
                            follow = None
                    finished = new_finished

                    # Start new workers if possible
                    while source_iter is not None and len(workers) < opt.nthreads:
                        try:
                            source = source_iter.next()
                        except StopIteration:
                            source_iter = None
                        else:
                            # Start a new worker.
                            w = DocTestWorker(source, options=opt, funclist=[sel_exit])
                            w.heading = self.controller.reporter.report_head(w.source)
                            if opt.nthreads == 1:
                                # With 1 process, the child prints
                                # directly to stdout, so we need to
                                # log the heading now.
                                log(w.heading)
                                w.heading = None
                            w.start()  # This might take some time
                            w.deadline = time.time() + opt.timeout
                            workers.append(w)
                            restart = True

                    # Recompute state if needed
                    if restart:
                        continue

                    # We are finished if there are no DocTestWorkers left
                    if len(workers) == 0:
                        # If there are no active workers, we should have
                        # reported all finished workers.
                        assert len(finished) == 0
                        break

                    # The master pselect() call
                    rlist = [w.rmessages for w in workers if w.rmessages is not None]
                    tmout = min(w.deadline for w in workers) - now
                    if tmout > 5:  # Wait at most 5 seconds
                        tmout = 5
                    rlist, _, _, _ = sel.pselect(rlist, timeout=tmout)

                    # Read messages
                    for w in workers:
                        if w.rmessages is not None and w.rmessages in rlist:
                            w.read_messages()

                    # Find a worker to follow: if there is only one worker,
                    # always follow it. Otherwise, take the worker with
                    # the earliest deadline of all workers with messages.
                    if follow is None:
                        if len(workers) == 1:
                            follow = workers[0]
                        else:
                            for w in workers:
                                if w.messages:
                                    if follow is None or w.deadline < follow.deadline:
                                        follow = w

                    # Write messages of followed worker
                    if follow is not None:
                        if follow.heading is not None:
                            log(follow.heading)
                            follow.heading = None
                        log(follow.messages, end="")
                        follow.messages = ""
        finally:
            # Restore SIGCHLD handler (which is to ignore the signal)
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)

            # Kill all remaining workers (in case we got interrupted)
            for w in workers:
                try:
                    w.kill()
                except Exception:
                    pass
                else:
                    log("Killing test %s"%w.source.printpath)
            # Fork a child process with the specific purpose of
            # killing the remaining workers.
            if len(workers) > 0 and os.fork() == 0:
                # Block these signals
                with PSelecter([signal.SIGHUP, signal.SIGINT]):
                    try:
                        from time import sleep
                        sleep(die_timeout)
                        for w in workers:
                            try:
                                w.kill()
                            except Exception:
                                pass
                    finally:
                        os._exit(0)

            # Hack to ensure multiprocessing leaves these processes
            # alone (in particular, it doesn't wait for them when we
            # exit).
            multiprocessing.current_process()._children = set()

    def dispatch(self):
        """
        Run the doctests for the controller's specified sources,
        by calling :meth:`parallel_dispatch` or :meth:`serial_dispatch`
        according to the ``--serial`` option.

        EXAMPLES::

            sage: from sage.doctest.control import DocTestController, DocTestDefaults
            sage: from sage.doctest.forker import DocTestDispatcher
            sage: from sage.doctest.reporting import DocTestReporter
            sage: from sage.doctest.util import Timer
            sage: from sage.env import SAGE_SRC
            sage: import os
            sage: freehom = os.path.join(SAGE_SRC, 'sage', 'modules', 'free_module_homspace.py')
            sage: bigo = os.path.join(SAGE_SRC, 'sage', 'rings', 'big_oh.py')
            sage: DC = DocTestController(DocTestDefaults(), [freehom, bigo])
            sage: DC.expand_files_into_sources()
            sage: DD = DocTestDispatcher(DC)
            sage: DR = DocTestReporter(DC)
            sage: DC.reporter = DR
            sage: DC.dispatcher = DD
            sage: DC.timer = Timer().start()
            sage: DD.dispatch()
            sage -t .../sage/modules/free_module_homspace.py
                [... tests, ... s]
            sage -t .../sage/rings/big_oh.py
                [... tests, ... s]
        """
        if self.controller.options.serial:
            self.serial_dispatch()
        else:
            self.parallel_dispatch()


class DocTestWorker(multiprocessing.Process):
    """
    The DocTestWorker process runs one :class:`DocTestTask` for a given
    source. It returns messages about doctest failures (or all tests if
    verbose doctesting) though a pipe and returns results through a
    ``multiprocessing.Queue`` instance (both these are created in the
    :meth:`start` method).

    It runs the task in its own process-group, such that killing the
    process group kills this process together with its child processes.

    The class has additional methods and attributes for bookkeeping
    by the master process. Except in :meth:`run`, nothing from this
    class should be accessed by the child process.

    INPUT:

    - ``source`` -- a :class:`DocTestSource` instance

    - ``options`` -- an object representing doctest options.

    - ``funclist`` -- a list of callables to be called at the start of
      the child process.

    EXAMPLES::

        sage: from sage.doctest.forker import DocTestWorker, DocTestTask
        sage: from sage.doctest.sources import FileDocTestSource
        sage: from sage.doctest.reporting import DocTestReporter
        sage: from sage.doctest.control import DocTestController, DocTestDefaults
        sage: from sage.env import SAGE_SRC
        sage: filename = os.path.join(SAGE_SRC,'sage','doctest','util.py')
        sage: DD = DocTestDefaults()
        sage: FDS = FileDocTestSource(filename,DD)
        sage: W = DocTestWorker(FDS, DD)
        sage: W.start()
        sage: DC = DocTestController(DD, filename)
        sage: reporter = DocTestReporter(DC)
        sage: W.join()  # Wait for worker to finish
        sage: result = W.result_queue.get()
        sage: reporter.report(FDS, False, W.exitcode, result, "")
            [... tests, ... s]
    """
    def __init__(self, source, options, funclist=[]):
        """
        Initialization.

        TESTS::

            sage: run_doctests(sage.rings.big_oh) # indirect doctest
            Running doctests with ID ...
            Doctesting 1 file.
            sage -t .../sage/rings/big_oh.py
                [... tests, ... s]
            ----------------------------------------------------------------------
            All tests passed!
            ----------------------------------------------------------------------
            Total time for all tests: ... seconds
                cpu time: ... seconds
                cumulative wall time: ... seconds
        """
        multiprocessing.Process.__init__(self)

        self.source = source
        self.options = options
        self.funclist = funclist

        # Open pipe for messages. These are raw file descriptors,
        # not Python file objects!
        self.rmessages, self.wmessages = os.pipe()

        # Create Queue for the result. Since we're running only one
        # doctest, this "queue" will contain only 1 element.
        self.result_queue = multiprocessing.Queue(1)

        # Temporary file for stdout/stderr of the child process.
        # Normally, this isn't used in the master process except to
        # debug timeouts/crashes.
        self.outtmpfile = os.tmpfile()

        # Create string for the master process to store the messages
        # (usually these are the doctest failures) of the child.
        # These messages are read through the pipe created above.
        self.messages = ""

        # Has this worker been killed (because of a time out)?
        self.killed = False

    def run(self):
        """
        Runs the :class:`DocTestTask` under its own PGID.

        TESTS::

            sage: run_doctests(sage.symbolic.units) # indirect doctest
            Running doctests with ID ...
            Doctesting 1 file.
            sage -t .../sage/symbolic/units.py
                [... tests, ... s]
            ----------------------------------------------------------------------
            All tests passed!
            ----------------------------------------------------------------------
            Total time for all tests: ... seconds
                cpu time: ... seconds
                cumulative wall time: ... seconds
        """
        os.setpgid(os.getpid(), os.getpid())

        # Run functions
        for f in self.funclist:
            f()

        # Write one byte to the pipe to signal to the master process
        # that we have started properly.
        os.write(self.wmessages, "X")

        task = DocTestTask(self.source)

        # Ensure the Python stdin is the actual stdin
        # (multiprocessing redirects this).
        # We will do a more proper redirect of stdin in SageSpoofInOut.
        try:
            sys.stdin = os.fdopen(0, "r")
        except OSError:
            # We failed to open stdin for reading, this might happen
            # for example when running under "nohup" (Trac #14307).
            # Simply redirect stdin from /dev/null and try again.
            with open(os.devnull) as f:
                os.dup2(f.fileno(), 0)
            sys.stdin = os.fdopen(0, "r")

        # Close the reading end of the pipe (only the master should
        # read from the pipe) and open the writing end.
        os.close(self.rmessages)
        msgpipe = os.fdopen(self.wmessages, "w")
        try:
            # If we have only one process, we print directly to stdout
            # without using the message pipe. This is mainly needed to
            # support the --debug option.
            if self.options.nthreads == 1:
                msgpipe.close()
                msgpipe = None

            task(self.options, self.outtmpfile, msgpipe, self.result_queue)
        finally:
            if msgpipe is not None:
                msgpipe.close()

    def start(self):
        """
        Start the worker and close the writing end of the message pipe.

        TESTS::

            sage: from sage.doctest.forker import DocTestWorker, DocTestTask
            sage: from sage.doctest.sources import FileDocTestSource
            sage: from sage.doctest.reporting import DocTestReporter
            sage: from sage.doctest.control import DocTestController, DocTestDefaults
            sage: from sage.env import SAGE_SRC
            sage: filename = os.path.join(SAGE_SRC,'sage','doctest','util.py')
            sage: DD = DocTestDefaults()
            sage: FDS = FileDocTestSource(filename,DD)
            sage: W = DocTestWorker(FDS, DD)
            sage: W.start()
            sage: try:
            ....:     os.fstat(W.wmessages)
            ....: except OSError:
            ....:     print "Write end of pipe successfully closed"
            Write end of pipe successfully closed
            sage: W.join()  # Wait for worker to finish
        """
        super(DocTestWorker, self).start()

        # Close the writing end of the pipe (only the child should
        # write to the pipe).
        os.close(self.wmessages)

        # Read one byte from the pipe as a sign that the child process
        # has properly started (to avoid race conditions). In particular,
        # it will have its process group changed.
        os.read(self.rmessages, 1)

    def read_messages(self):
        """
        In the master process, read from the pipe and store the data
        read in the ``messages`` attribute.

        .. NOTE::

            This function may need to be called multiple times in
            order to read all of the messages.

        EXAMPLES::

            sage: from sage.doctest.forker import DocTestWorker, DocTestTask
            sage: from sage.doctest.sources import FileDocTestSource
            sage: from sage.doctest.reporting import DocTestReporter
            sage: from sage.doctest.control import DocTestController, DocTestDefaults
            sage: from sage.env import SAGE_SRC
            sage: filename = os.path.join(SAGE_SRC,'sage','doctest','util.py')
            sage: DD = DocTestDefaults(verbose=True,nthreads=2)
            sage: FDS = FileDocTestSource(filename,DD)
            sage: W = DocTestWorker(FDS, DD)
            sage: W.start()
            sage: while W.rmessages is not None:
            ....:     W.read_messages()
            sage: W.join()
            sage: len(W.messages) > 0
            True
        """
        # It's absolutely important to execute only one read() system
        # call, more might block. Assuming that we used pselect()
        # correctly, one read() will not block.
        if self.rmessages is not None:
            s = os.read(self.rmessages, 4096)
            self.messages += s
            if len(s) == 0:  # EOF
                os.close(self.rmessages)
                self.rmessages = None

    def save_result_output(self):
        """
        Annotate ``self`` with ``self.result`` (the result read through
        the ``result_queue`` and with ``self.output``, the complete
        contents of ``self.outtmpfile``. Then close the Queue and
        ``self.outtmpfile``.

        EXAMPLES::

            sage: from sage.doctest.forker import DocTestWorker, DocTestTask
            sage: from sage.doctest.sources import FileDocTestSource
            sage: from sage.doctest.reporting import DocTestReporter
            sage: from sage.doctest.control import DocTestController, DocTestDefaults
            sage: from sage.env import SAGE_SRC
            sage: filename = os.path.join(SAGE_SRC,'sage','doctest','util.py')
            sage: DD = DocTestDefaults()
            sage: FDS = FileDocTestSource(filename,DD)
            sage: W = DocTestWorker(FDS, DD)
            sage: W.start()
            sage: W.join()
            sage: W.save_result_output()
            sage: sorted(W.result[1].keys())
            ['cputime', 'err', 'failures', 'optionals', 'walltime']
            sage: len(W.output) > 0
            True
        """
        from Queue import Empty
        try:
            self.result = self.result_queue.get(block=False)
        except Empty:
            self.result = (0, DictAsObject(dict(err='noresult')))
        del self.result_queue

        self.outtmpfile.seek(0)
        self.output = self.outtmpfile.read()
        del self.outtmpfile

    def kill(self):
        """
        Kill this worker. The first time this is called, use
        ``SIGHUP``. Subsequent times, use ``SIGKILL``.  Also close the
        message pipe if it was still open.

        EXAMPLES::

            sage: import time
            sage: from sage.doctest.forker import DocTestWorker, DocTestTask
            sage: from sage.doctest.sources import FileDocTestSource
            sage: from sage.doctest.reporting import DocTestReporter
            sage: from sage.doctest.control import DocTestController, DocTestDefaults
            sage: from sage.env import SAGE_SRC
            sage: filename = os.path.join(SAGE_SRC,'sage','doctest','tests','99seconds.rst')
            sage: DD = DocTestDefaults()
            sage: FDS = FileDocTestSource(filename,DD)

        We set up the worker to start by blocking ``SIGHUP``, such that
        killing will fail initially::

            sage: from sage.ext.pselect import PSelecter
            sage: import signal
            sage: def block_hup():
            ....:     # We never __exit__()
            ....:     PSelecter([signal.SIGHUP]).__enter__()
            sage: W = DocTestWorker(FDS, DD, [block_hup])
            sage: W.start()
            sage: W.killed
            False
            sage: W.kill()
            sage: W.killed
            True
            sage: time.sleep(0.2)  # Worker doesn't die
            sage: W.kill()         # Worker dies now
            sage: time.sleep(0.2)
            sage: W.is_alive()
            False
        """
        if self.rmessages is not None:
            os.close(self.rmessages)
            self.rmessages = None
        if not self.killed:
            self.killed = True
            os.killpg(self.pid, signal.SIGHUP)
        else:
            os.killpg(self.pid, signal.SIGKILL)


class DocTestTask(object):
    """
    This class encapsulates the tests from a single source.

    This class does not insulate from problems in the source
    (e.g. entering an infinite loop or causing a segfault), that has to
    be dealt with at a higher level.

    INPUT:

    - ``source`` -- a :class:`sage.doctest.sources.DocTestSource` instance.

    - ``verbose`` -- boolean, controls reporting of progress by :class:`doctest.DocTestRunner`.

    EXAMPLES::

        sage: from sage.doctest.forker import DocTestTask
        sage: from sage.doctest.sources import FileDocTestSource
        sage: from sage.doctest.control import DocTestDefaults, DocTestController
        sage: from sage.env import SAGE_SRC
        sage: import os
        sage: filename = os.path.join(SAGE_SRC,'sage','doctest','sources.py')
        sage: DD = DocTestDefaults()
        sage: FDS = FileDocTestSource(filename,DD)
        sage: DTT = DocTestTask(FDS)
        sage: DC = DocTestController(DD,[filename])
        sage: ntests, results = DTT(options=DD)
        sage: ntests >= 300 or ntests
        True
        sage: sorted(results.keys())
        ['cputime', 'err', 'failures', 'optionals', 'walltime']
    """
    def __init__(self, source):
        """
        Initialization.

        TESTS::

            sage: from sage.doctest.forker import DocTestTask
            sage: from sage.doctest.sources import FileDocTestSource
            sage: from sage.doctest.control import DocTestDefaults
            sage: from sage.env import SAGE_SRC
            sage: import os
            sage: filename = os.path.join(SAGE_SRC,'sage','doctest','sources.py')
            sage: FDS = FileDocTestSource(filename,DocTestDefaults())
            sage: DocTestTask(FDS)
            <sage.doctest.forker.DocTestTask object at ...>
        """
        self.source = source

    def __call__(self, options, outtmpfile=None, msgfile=None, result_queue=None):
        """
        Calling the task does the actual work of running the doctests.

        INPUT:

        - ``options`` -- an object representing doctest options.

        - ``outtmpfile`` -- a seekable file that's used by the doctest
          runner to redirect stdout and stderr.

        - ``msgfile`` -- a file or pipe to send doctest messages about
          doctest failures (or all tests in verbose mode).

        - ``result_queue`` -- an instance of :class:`multiprocessing.Queue`
          to store the doctest result. For testing, this can also be None.

        OUPUT:

        - ``(doctests, result_dict)`` where ``doctests`` is the number of
          doctests and and ``result_dict`` is a dictionary annotated with
          timings and error information.

        - Also put ``(doctests, result_dict)`` onto the ``result_queue``
          if the latter isn't None.

        EXAMPLES::

            sage: from sage.doctest.forker import DocTestTask
            sage: from sage.doctest.sources import FileDocTestSource
            sage: from sage.doctest.control import DocTestDefaults, DocTestController
            sage: from sage.env import SAGE_SRC
            sage: import os
            sage: filename = os.path.join(SAGE_SRC,'sage','doctest','parsing.py')
            sage: DD = DocTestDefaults()
            sage: FDS = FileDocTestSource(filename,DD)
            sage: DTT = DocTestTask(FDS)
            sage: DC = DocTestController(DD, [filename])
            sage: ntests, runner = DTT(options=DD)
            sage: runner.failures
            0
            sage: ntests >= 200 or ntests
            True
        """
        # Store all existing atexit() functions and clear them,
        # so we know that all newly registered functions come from
        # doctests.
        import atexit
        saved_exithandlers = atexit._exithandlers
        atexit._exithandlers = []

        result = None
        try:
            file = self.source.path
            basename = self.source.basename
            import sage.all_cmdline
            runner = SageDocTestRunner(
                    SageOutputChecker(),
                    verbose=options.verbose,
                    outtmpfile=outtmpfile,
                    msgfile=msgfile,
                    sage_options=options,
                    optionflags=doctest.NORMALIZE_WHITESPACE|doctest.ELLIPSIS)
            runner.basename = basename
            N = options.file_iterations
            results = DictAsObject(dict(walltime=[],cputime=[],err=None))
            for it in range(N):
                sage_namespace = RecordingDict(dict(sage.all_cmdline.__dict__))
                sage_namespace['__name__'] = '__main__'
                sage_namespace['__package__'] = None
                doctests, extras = self.source.create_doctests(sage_namespace)
                timer = Timer().start()

                for test in doctests:
                    runner.run(test)
                runner.filename = file
                failed, tried = runner.summarize(options.verbose)
                timer.stop().annotate(runner)
                if runner.update_results(results):
                    break
            if extras['tab']:
                results.err = 'tab'
                results.tab_linenos = extras['tab']
            results.optionals = extras['optionals']
            # We subtract 1 to remove the sig_on_count() tests
            result = (sum([max(0,len(test.examples) - 1) for test in doctests]), results)

            # multiprocessing.Process instances don't run exit
            # functions, so we run the functions added by doctests
            # now manually and restore the old exit functions.
            atexit._run_exitfuncs()
            atexit._exithandlers = saved_exithandlers
        except BaseException:
            exc_info = sys.exc_info()
            tb = "".join(traceback.format_exception(*exc_info))
            result = (0, DictAsObject(dict(err=exc_info[0], tb=tb)))

        if result_queue is not None:
            result_queue.put(result, False)
        return result
