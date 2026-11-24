/* A PEP 523 frame-evaluation hook that only passes each frame on to
   CPython's own evaluator: plain CPython that pays for having a hook
   installed (from 3.11, calls it can't specialise or inline), to compare
   LeJIT with like for like. pyperf37.py:build_passhook builds it, with
   HAVE_SET_EVAL_FRAME_FUNC when this CPython has the setter (whose hook takes
   the thread state first); without it the hook is the interpreter's
   eval_frame field. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#ifdef HAVE_SET_EVAL_FRAME_FUNC

/* the frame is a PyFrameObject or a _PyInterpreterFrame: only passed on */
typedef PyObject *(*eval_fn)(PyThreadState *, void *, int);
static eval_fn original;

static PyObject *
pass_on(PyThreadState *tstate, void *frame, int throwflag)
{
    return original(tstate, frame, throwflag);
}

static void
set_hook(void)
{
    PyInterpreterState *interp = PyThreadState_Get()->interp;
    original = (eval_fn)_PyInterpreterState_GetEvalFrameFunc(interp);
    _PyInterpreterState_SetEvalFrameFunc(interp, (void *)pass_on);
}

#else

#include <frameobject.h>
#if defined(__has_include)
#if __has_include(<internal/pycore_pystate.h>)
#define Py_BUILD_CORE
#include <internal/pycore_pystate.h>
#endif
#endif

static _PyFrameEvalFunction original;

static PyObject *
pass_on(PyFrameObject *frame, int throwflag)
{
    return original(frame, throwflag);
}

static void
set_hook(void)
{
    PyInterpreterState *interp = PyThreadState_Get()->interp;
    original = interp->eval_frame;
    interp->eval_frame = pass_on;
}

#endif

static PyObject *
install(PyObject *self, PyObject *unused)
{
    set_hook();
    Py_RETURN_NONE;
}

static PyMethodDef methods[] = {{"install", install, METH_NOARGS, NULL}, {NULL}};
static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "passhook", NULL, -1, methods};

PyMODINIT_FUNC
PyInit_passhook(void)
{
    return PyModule_Create(&module);
}
