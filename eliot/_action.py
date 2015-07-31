"""
Support for actions and tasks.

Actions have a beginning and an eventual end, and can be nested. Tasks are
top-level actions.
"""

from __future__ import unicode_literals, absolute_import

import threading
from uuid import uuid4
from itertools import count
from contextlib import contextmanager
from warnings import warn

from pyrsistent import pvector

from six import text_type as unicode

from ._message import Message
from ._util import safeunicode


class _ExecutionContext(threading.local):
    """
    Call stack-based context, storing the current L{Action}.

    Bit like L{twisted.python.context}, but:

    - Single purpose.
    - Allows support for Python context managers (this could easily be added
      to Twisted, though).
    - Does not require Twisted; Eliot should not require Twisted if possible.
    """
    def __init__(self):
        self._stack = []


    def push(self, action):
        """
        Push the given L{Action} to the front of the stack.

        @param action: L{Action} that will be used for log messages and as
            parent of newly created L{Action} instances.
        """
        self._stack.append(action)


    def pop(self):
        """
        Pop the front L{Action} on the stack.
        """
        self._stack.pop(-1)


    def current(self):
        """
        @return: The current front L{Action}, or C{None} if there is no
            L{Action} set.
        """
        if not self._stack:
            return None
        return self._stack[-1]


_context = _ExecutionContext()
currentAction = _context.current



class TaskLevel(object):
    """
    The location of a message within the tree of actions of a task.

    @ivar level: A pvector of integers. Each item indicates a child
        relationship, and the value indicates message count. E.g. C{[2,
        3]} indicates this is the third message within an action which is
        the second item in the task.

    @ivar _numberOfMessages: The number of messages created in the context of
        an action.
    """

    def __init__(self, level):
        self.level = pvector(level)
        self._numberOfMessages = iter(count())

    def __eq__(self, other):
        return self.level == other.level

    def __ne__(self, other):
        return not (self == other)

    @classmethod
    def fromString(cls, string):
        """
        Convert a serialized Unicode string to a L{TaskLevel}.

        @param string: Output of L{TaskLevel.toString}.

        @return: L{TaskLevel} parsed from the string.
        """
        return cls(level=pvector([int(i) for i in string.split("/") if i]))


    def toString(self):
        """
        Convert to a Unicode string, for serialization purposes.

        @return: L{unicode} representation of the L{TaskLevel}.
        """
        return "/" + "/".join(map(unicode, self.level))


    def next(self):
        """
        Return the next L{TaskLevel}.

        @return: L{TaskLevel} which follows this one.
        """
        return TaskLevel(level=self.level.set(-1, self.level[-1] + 1))


    def child(self, level=1):
        """
        Return a child of this L{TaskLevel}.

        @param int level: Which child to return. Defaults to 1, the first
            child.
        @return: L{TaskLevel} which is a child of this one.
        """
        return TaskLevel(level=self.level.append(level))


    def parent(self):
        """
        Return the parent of this L{TaskLevel}, or C{None} if it doesn't have
        one.

        @return: L{TaskLevel} which is the parent of this one.
        """
        if not self.level:
            return None
        return TaskLevel(level=self.level[:-1])


    def nextChild(self):
        """
        Return the next child L{TaskLevel}.

        @return: L{TaskLevel} which is child of this one.
        """
        return self.child(next(self._numberOfMessages) + 1)


    # PEP 8 compatibility:
    from_string = fromString
    to_string = toString
    next_child = nextChild


_TASK_ID_NOT_SUPPLIED = object()


class Action(object):
    """
    Part of a nested heirarchy of ongoing actions.

    An action has a start and an end; a message is logged for each.

    Actions should only be used from a single thread, by implication the
    thread where they were created.

    @ivar _identification: Fields identifying this action.

    @ivar _successFields: Fields to be included in successful finish message.

    @ivar _finished: L{True} if the L{Action} has finished, otherwise L{False}.
    """
    def __init__(self, logger, task_uuid, task_level, action_type,
                 serializers=None):
        """
        Initialize the L{Action} and log the start message.

        You probably do not want to use this API directly: use L{startAction}
        or L{startTask} instead.

        @param logger: The L{eliot.ILogger} to which to write
            messages.

        @param task_uuid: The uuid of the top-level task, e.g. C{"123525"}.

        @param task_level: The action's level in the task.
        @type task_level: L{TaskLevel}

        @param action_type: The type of the action,
            e.g. C{"yourapp:subsystem:dosomething"}.

        @param serializers: Either a L{eliot._validation._ActionSerializers}
            instance or C{None}. In the latter case no validation or
            serialization will be done for messages generated by the
            L{Action}.
        """
        self._numberOfMessages = iter(count())
        self._successFields = {}
        self._logger = logger
        if isinstance(task_level, unicode):
            warn("Action should be initialized with a TaskLevel",
                 DeprecationWarning, stacklevel=2)
            task_level = TaskLevel.fromString(task_level)
        self._task_level = task_level
        self._last_child = None
        self._identification = {"task_uuid": task_uuid,
                                "action_type": action_type,
                                }
        self._serializers = serializers
        self._finished = False


    def serializeTaskId(self):
        """
        Create a unique identifier for the current location within the task.

        The format is C{b"<task_uuid>@<task_level>"}.

        @return: L{bytes} encoding the current location within the task.
        """
        return "{}@{}".format(self._identification["task_uuid"],
                              self._nextTaskLevel().toString()).encode("ascii")


    @classmethod
    def continueTask(cls, logger=None, task_id=_TASK_ID_NOT_SUPPLIED):
        """
        Start a new action which is part of a serialized task.

        @param logger: The L{eliot.ILogger} to which to write
            messages, or C{None} if the default one should be used.

        @param task_id: A serialized task identifier, the output of
            L{Action.serialize_task_id}. Required.

        @return: The new L{Action} instance.
        """
        if task_id is _TASK_ID_NOT_SUPPLIED:
            raise RuntimeError("You must supply a task_id keyword argument.")
        uuid, task_level = task_id.decode("ascii").split("@")
        action = cls(logger, uuid, TaskLevel.fromString(task_level),
                     "eliot:remote_task")
        action._start({})
        return action


    # PEP 8 variants:
    serialize_task_id = serializeTaskId
    continue_task = continueTask


    def _nextTaskLevel(self):
        """
        Return the next C{task_level} for messages within this action.

        Called whenever a message is logged within the context of an action.

        @return: The message's C{task_level}.
        """
        if not self._last_child:
            self._last_child = self._task_level.child()
        else:
            self._last_child = self._last_child.next()
        return self._last_child


    def _start(self, fields):
        """
        Log the start message.

        The action identification fields, and any additional given fields,
        will be logged.

        In general you shouldn't call this yourself, instead using a C{with}
        block or L{Action.finish}.
        """
        fields["action_status"] = "started"
        fields.update(self._identification)
        if self._serializers is None:
            serializer = None
        else:
            serializer = self._serializers.start
        Message(fields, serializer).write(self._logger, self)


    def finish(self, exception=None):
        """
        Log the finish message.

        The action identification fields, and any additional given fields,
        will be logged.

        In general you shouldn't call this yourself, instead using a C{with}
        block or L{Action.finish}.

        @param exception: C{None}, in which case the fields added with
            L{Action.addSuccessFields} are used. Or an L{Exception}, in
            which case an C{"exception"} field is added with the given
            L{Exception} type and C{"reason"} with its contents.
        """
        if self._finished:
            return
        self._finished = True
        serializer = None
        if exception is None:
            fields = self._successFields
            fields["action_status"] = "succeeded"
            if self._serializers is not None:
                serializer = self._serializers.success
        else:
            fields = {}
            fields["exception"] = "%s.%s" % (exception.__class__.__module__,
                                             exception.__class__.__name__)
            fields["reason"] = safeunicode(exception)
            fields["action_status"] = "failed"
            if self._serializers is not None:
                serializer = self._serializers.failure

        fields.update(self._identification)
        Message(fields, serializer).write(self._logger, self)


    def child(self, logger, action_type, serializers=None):
        """
        Create a child L{Action}.

        Rather than calling this directly, you can use L{startAction} to
        create child L{Action} using the execution context.

        @param logger: The L{eliot.ILogger} to which to write
            messages.

        @param action_type: The type of this action,
            e.g. C{"yourapp:subsystem:dosomething"}.

        @param serializers: Either a L{eliot._validation._ActionSerializers}
            instance or C{None}. In the latter case no validation or
            serialization will be done for messages generated by the
            L{Action}.
        """
        newLevel = self._nextTaskLevel()
        return self.__class__(logger,
                              self._identification["task_uuid"],
                              newLevel,
                              action_type,
                              serializers)


    def run(self, f, *args, **kwargs):
        """
        Run the given function with this L{Action} as its execution context.
        """
        _context.push(self)
        try:
            return f(*args, **kwargs)
        finally:
            _context.pop()


    def addSuccessFields(self, **fields):
        """
        Add fields to be included in the result message when the action
        finishes successfully.

        @param fields: Additional fields to add to the result message.
        """
        self._successFields.update(fields)


    # PEP 8 variant:
    add_success_fields = addSuccessFields


    @contextmanager
    def context(self):
        """
        Create a context manager that ensures code runs within action's context.

        The action does NOT finish when the context is exited.
        """
        _context.push(self)
        try:
            yield
        finally:
            _context.pop()


    # Python context manager implementation:
    def __enter__(self):
        """
        Push this action onto the execution context.
        """
        _context.push(self)
        return self


    def __exit__(self, type, exception, traceback):
        """
        Pop this action off the execution context, log finish message.
        """
        _context.pop()
        self.finish(exception)



def startAction(logger=None, action_type="", _serializers=None, **fields):
    """
    Create a child L{Action}, figuring out the parent L{Action} from execution
    context, and log the start message.

    You can use the result as a Python context manager, or use the
    L{Action.finish} API to explicitly finish it.

         with startAction(logger, "yourapp:subsystem:dosomething",
                          entry=x) as action:
              do(x)
              result = something(x * 2)
              action.addSuccessFields(result=result)

    Or alternatively:

         action = startAction(logger, "yourapp:subsystem:dosomething",
                              entry=x)
         with action.context():
              do(x)
              result = something(x * 2)
              action.addSuccessFields(result=result)
         action.finish()

    @param logger: The L{eliot.ILogger} to which to write messages, or
        C{None} to use the default one.

    @param action_type: The type of this action,
        e.g. C{"yourapp:subsystem:dosomething"}.

    @param _serializers: Either a L{eliot._validation._ActionSerializers}
        instance or C{None}. In the latter case no validation or serialization
        will be done for messages generated by the L{Action}.

    @param fields: Additional fields to add to the start message.

    @return: A new L{Action}.
    """
    parent = currentAction()
    if parent is None:
        return startTask(logger, action_type, _serializers, **fields)
    else:
        action = parent.child(logger, action_type, _serializers)
        action._start(fields)
        return action



def startTask(logger=None, action_type=u"", _serializers=None, **fields):
    """
    Like L{action}, but creates a new top-level L{Action} with no parent.

    @param logger: The L{eliot.ILogger} to which to write messages, or
        C{None} to use the default one.

    @param action_type: The type of this action,
        e.g. C{"yourapp:subsystem:dosomething"}.

    @param _serializers: Either a L{eliot._validation._ActionSerializers}
        instance or C{None}. In the latter case no validation or serialization
        will be done for messages generated by the L{Action}.

    @param fields: Additional fields to add to the start message.

    @return: A new L{Action}.
    """
    action = Action(logger, unicode(uuid4()), TaskLevel(level=[]), action_type,
                    _serializers)
    action._start(fields)
    return action
