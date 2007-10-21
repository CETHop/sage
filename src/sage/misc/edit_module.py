"""
edit_module

AUTHOR: * Nils Bruin
        * William Stein -- touch up for inclusion in Sage.

This module provides a routine to open the source file of a python
object in an editor of your choice, if the source file can be figured
out.  For files that appear to be from the sage library, the path name
gets modified to the corresponding file in the current branch, i.e.,
the file that gets copied into the library upon 'sage -br'.

The editor to be run, and the way it should be called to open the
requested file at the right line number, can be supplied via a
template. For a limited number of editors, templates are already known
to the system. In those cases it suffices to give the editor name.

In fact, if the environment variable EDITOR is set to a known editor,
then the system will use that if no template has been set explicitly.
"""

######################################################################
#  Copyright (C) 2007 Nils Bruin <nbruin@sfu.ca> and
#                     William Stein <wstein@math.ucsd.edu>
#
#  Distributed under the terms of the GNU General Public License (GPL)
#
#    This code is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty
#    of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
#  See the GNU General Public License for more details; the full text
#  is available at:
#
#                  http://www.gnu.org/licenses/
######################################################################


import sage.misc.sageinspect
import inspect
import os
import sys

from string import Template

#by default we do not have an edit template
edit_template = None

#we can set some defaults, however. Add

template_defaults = {
      'vi'       : Template('vi -c ${line} ${file}'),
      'vim'      : Template('vim -c ${line} ${file}'),
      'emacs'    : Template('emacs ${opts} +${line} ${file}'),
      'nedit-nc' : Template('nedit-nc -line ${line} ${file}'),
      'gedit'    : Template('gedit +${line} ${file}')
   }

def file_and_line(obj):
   r"""
   Look up source file and line number of obj.

   If the file lies in the sage library, the pathname of the
   corresponding file in the current branch (i.e., the file that gets
   copied into the sage library upon running 'sage -br').  Note that
   the first line of a file is considered to be 1 rather than 0
   because most editors think that this is the case.

   AUTHOR:
      Nils Bruin (2007-10-03)

   EXAMPLE:
      sage: import edit_module
      sage: edit_module.file_and_line(sage)     # random output
      ('/usr/local/sage/default/devel/sage/sage/__init__.py', 1)
   """
   d = inspect.getdoc(obj)
   ret = sage.misc.sageinspect._extract_embedded_position(d);
   if ret is not None:
     (_, filename, lineno) = ret
   else:
     filename = inspect.getsourcefile(obj)
     _,lineno = inspect.findsource(obj)
     #
     #  for sage files, the registered source file is the result of the preparsing
     #  these files end in ".py" and have "*autogenerated*" on the second line
     #  for those files, we replace the extension by ".sage" and we subtract
     #  3 from the line number to compensate for the 3 lines that were prefixed
     #  in the preparsing process
     #
     if filename[-3:] == '.py':
       infile=open(filename,'r')
       infile.readline()
       if infile.readline().find("*autogenerated*") >= 0:
         filename=filename[:-3]+'.sage'
	 lineno = lineno-3

   sageroot = sage.misc.sageinspect.SAGE_ROOT+'/'
   runbranches = ['local/lib/python/site-packages',
                  'local/lib/python2.5/site-packages']
   develbranch = 'devel/sage'
   for runbranch in runbranches:
     prefix = sageroot+runbranch
     if filename.startswith(prefix):
       filename = sageroot+develbranch+filename[len(prefix):]

   return filename, lineno+1

def set_edit_template(template_string):
   r"""
   Sets default edit template string.

   It should reference ${file} and ${line}. This routine normally
   needs to be called prior to using 'edit'. However, if the editor
   set in the shell variable EDITOR is known, then the system will
   substitute an appropriate template for you. See
   edit_module.template_defaults for the recognised templates.

   AUTHOR:
      Nils Bruin (2007-10-03)

   EXAMPLE:
      sage: from sage.misc.edit_module import set_edit_template
      sage: set_edit_template("echo EDIT ${file}:${line}")
      sage: edit(sage)      # not tested
      EDIT /usr/local/sage/default/devel/sage/sage/__init__.py:1
   """
   global edit_template

   if template_string in template_defaults.keys():
      template_string = template_defaults[template_string]
   edit_template = Template(template_string)

def edit(obj, bg=False, editor=None):
   r"""
   Open source code of obj in editor of your choice.

   INPUT:
       bg -- bool (default: False); if True, then the
             editor is run in the background

   AUTHOR:
       Nils Bruin (2007-10-03)

   EXAMPLE:

   This is a typical example of how to use this routine.

      # make some object obj
      sage: edit(obj)    # not tested

   Now for more details and customization:

      sage: import sage.misc.edit_module as m
      sage: m.set_edit_template("vi -c ${line} ${file}")

   In fact, since vi is a well-known editor, you could also just use

      sage: m.set_edit_template("vi")

   To illustrate:

      sage: m.edit_template.template.template
      'vi -c ${line} ${file}'

   And if your environment variable EDITOR is set to a recognised
   editor, you would not have to set anything.

   To edit the source of an object, just type something like:

      sage: edit(edit)           # not tested
   """
   global edit_template

   try:
      if editor:
         ED = editor
      else:
         ED = os.environ['EDITOR']
      EDITOR = ED.split()
      base = EDITOR[0]
      opts = ' '.join(EDITOR[1:])
      edit_template = template_defaults[base]
   except (KeyError, IndexError):
      raise ValueError, "Use set_edit_template(<template_string>) to set a default"
   filename, lineno = file_and_line(obj)
   cmd = edit_template.substitute(opts = opts, line = lineno, file = filename)
   if bg:
      cmd += ' &'
   print cmd
   os.system(cmd)
