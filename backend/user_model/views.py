"""
API for user module
"""
import hashlib
import time
import uuid
import json
import logging
from functools import wraps
from urllib.parse import quote, unquote
import bcrypt
from django.http import JsonResponse
from django.views import View
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.utils.html import strip_tags
from django.db.utils import IntegrityError
from django.db.models import Q
from django.core import serializers
from django.core.paginator import Paginator
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.shortcuts import redirect, render
from oauth2_provider.models import get_application_model as _get_application_model
from oauth2_provider.models import get_access_token_model as _get_access_token_model
from oauth2_provider.models import Application, AccessToken
from oauth2_provider.views import ProtectedResourceView
from user_model.models import UserModel, UserType
from api.common import RESPONSE, OAUTH_LOGIN_URL, random_password, get_uuid
from config import USER_TOKEN_EXPIRE_TIME, CLOUD_SCHEDULER_API_SERVER_BASE_URL, DEFAULT_FROM_EMAIL

LOGGER = logging.getLogger(__name__)


def send_password_info_email(username, password, email, is_reset):
    try:
        msg = 'Your Cloud Scheduler admin user is created, please login and change your password.' if not is_reset \
            else 'Your Cloud Scheduler password is reset by Super Admin'
        html_content = render_to_string('email/password_info.html', {'username': username,
                                                                     'password': password,
                                                                     'msg': msg})
        text_content = strip_tags(html_content)
        send_mail('Cloud Scheduler {}'.format('Admin User Created' if not is_reset
                                              else 'Password Changed'),
                  text_content, DEFAULT_FROM_EMAIL, recipient_list=[email], fail_silently=False,
                  html_message=html_content)
        return True
    except Exception as ex:
        LOGGER.exception(ex)
        return False


def get_application_model(use_generic=True):
    if use_generic:
        return Application
    else:
        return _get_application_model()


def get_access_token_model(use_generic=True):
    if use_generic:
        return AccessToken
    else:
        return _get_access_token_model()


def user_passes_test(test_func, redirect_oauth):
    """
    Decorator for views that checks that the user passes the given test,
    redirecting to the log-in page if necessary. The test should be a callable
    that takes the user object and returns True if the user passes.
    """

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            ret = test_func(request)
            if isinstance(ret, UserModel):
                kwargs['__user'] = ret
                request.user = ret
                return view_func(request, *args, **kwargs)
            else:
                if redirect_oauth:
                    response = redirect(CLOUD_SCHEDULER_API_SERVER_BASE_URL + OAUTH_LOGIN_URL +
                                        '?next={}'.format(
                                            quote(
                                                CLOUD_SCHEDULER_API_SERVER_BASE_URL +
                                                request.get_full_path().lstrip('/'))))
                else:
                    response = None
                    if ret == 1:
                        response = JsonResponse(RESPONSE.UNAUTHORIZED)
                    elif ret == -1:
                        response = JsonResponse(RESPONSE.PERMISSION_DENIED)
                response.delete_cookie('username')
                response.delete_cookie('token')
                return response

        return _wrapped_view

    return decorator


def user_login_common_wrapper(request, wrapper, allow_cookie):
    if 'HTTP_X_ACCESS_TOKEN' in request.META.keys() and 'HTTP_X_ACCESS_USERNAME' in request.META.keys():
        ret = wrapper(request.META['HTTP_X_ACCESS_USERNAME'], request.META['HTTP_X_ACCESS_TOKEN'])
    else:
        ret = 1
    if not isinstance(ret, UserModel) and allow_cookie:
        # try cookie auth
        username = request.COOKIES.get('username', None)
        token = request.COOKIES.get('token', None)
        ret = wrapper(username, token)
    return ret


def login_required(function=None, redirect_enabled=False, allow_cookie=False):
    """
    Decorator for views that checks that the user is logged in
    """

    def test_login_wrapper(username, header_token):
        try:
            user = UserModel.objects.get(username=username)
            token = TokenManager.get_token(user)
            if token == header_token:
                TokenManager.update_token(user)
                return user
            else:
                return 1
        except Exception as ex:
            LOGGER.warning(ex)
            return 1

    actual_decorator = user_passes_test(lambda request:
                                        user_login_common_wrapper(request, test_login_wrapper, allow_cookie),
                                        redirect_enabled)
    if function:
        return actual_decorator(function)
    return actual_decorator


def permission_required(function=None, redirect_enabled=False, allow_cookie=False, need_super_admin=False):
    """
    Decorator for views that checks that the user is logged in
    """

    def test_permission_wrapper(username, header_token):
        try:
            user = UserModel.objects.get(username=username)
            token = TokenManager.get_token(user)
            if token == header_token:
                if user.user_type == UserType.SUPER_ADMIN or (
                        not need_super_admin and user.user_type == UserType.ADMIN):
                    TokenManager.update_token(user)
                    return user
                else:
                    return -1
            else:
                return 1
        except Exception as ex:
            LOGGER.warning(ex)
            return 1

    actual_decorator = user_passes_test(lambda request:
                                        user_login_common_wrapper(request, test_permission_wrapper, allow_cookie),
                                        redirect_enabled)
    if function:
        return actual_decorator(function)
    return actual_decorator


class TokenManager:
    @staticmethod
    def create_token(user, new=True):
        if not new and user.token_expire_time > time.time():
            token = user.token
            expire_time = max(round(time.time()) + USER_TOKEN_EXPIRE_TIME, user.token_expire_time)
        else:
            token = str(uuid.uuid1())
            expire_time = round(time.time()) + USER_TOKEN_EXPIRE_TIME
        user.token = token
        user.token_expire_time = expire_time
        try:
            user.save(force_update=True)
        except Exception as ex:
            LOGGER.error(ex)
        return token

    @staticmethod
    def update_token(user):
        user.token_expire_time = round(time.time()) + USER_TOKEN_EXPIRE_TIME
        try:
            user.save(force_update=True)
        except Exception as ex:
            LOGGER.error(ex)

    @staticmethod
    def get_token(user):
        try:
            if user.token_expire_time > time.time():
                return user.token
            else:
                return ''
        except Exception as ex:
            LOGGER.error(ex)


class UserLogin(View):
    """User login view"""

    def post(self, request):
        """
        @api {post} /user/login/ User login
        @apiName UserLogin
        @apiGroup User
        @apiVersion 0.1.0
        @apiPermission user

        @apiParam {String} username Specifies the username as the unique identification.
        @apiParam {String} password Specifies the password.
        @apiSuccess {Object} payload Response object
        @apiSuccess {String} payload.username Username
        @apiSuccess {String} payload.token Total element count
        @apiSuccess {String} payload.avatar Avatar source of user.
        @apiSuccess {String} payload.permission User permission, must be one of [user, admin, super]
        @apiParamExample {json} Request-Example:
        {
            "username": "123456",
            "password": "123456"
        }
        @apiUse Success
        @apiUse OperationFailed
        @apiUse InvalidRequest
        @apiUse ServerError
        """
        response = None
        try:
            request = json.loads(request.body)
            username = request.get('username', None)
            password = request.get('password', None)
            if username is None or password is None:
                raise ValueError()
            user = UserModel.objects.get(username=username)
            user.last_login = timezone.now()
            user.save(force_update=True)
            password = bcrypt.hashpw(password.encode('utf-8'), user.salt.encode('utf-8')).decode('utf-8')
            md5 = hashlib.md5()
            md5.update(user.email.encode('utf-8'))
            if password == user.password:
                token = TokenManager.create_token(user)
                response = RESPONSE.SUCCESS
                response['payload'] = {
                    'username': user.username,
                    'uuid': user.uuid,
                    'token': token,
                    'avatar': 'https://fdn.geekzu.org/avatar/{}'.format(md5.hexdigest()),
                    'permission': {0: 'user', 1: 'admin', 2: 'super'}[user.user_type]
                }
            else:
                raise UserModel.DoesNotExist()
        except UserModel.DoesNotExist:
            response = RESPONSE.OPERATION_FAILED
            response['message'] += " {}".format("Invalid username or password.")
        except ValueError:
            response = RESPONSE.INVALID_REQUEST
        except Exception as ex:
            LOGGER.error(ex)
            response = RESPONSE.SERVER_ERROR
        finally:
            return JsonResponse(response)


class UserHandler(View):
    http_method_names = ['get', 'put', 'delete', 'post']

    @method_decorator(login_required)
    def get(self, _, **kwargs):
        """
        @api {get} /user/ Get user info
        @apiName GetUserInfo
        @apiGroup User
        @apiVersion 0.1.0
        @apiPermission user
        @apiDescription Get the detailed info of current user

        @apiSuccess {Object} payload Response object
        @apiSuccess {String} payload.username Username
        @apiSuccess {String} payload.email Email of current user
        @apiSuccess {String} payload.create_time Creation time of current user
        @apiSuccess {String} payload.uuid UUID of current user
        @apiUse APIHeader
        @apiUse Success
        @apiUse InvalidRequest
        @apiUse Unauthorized
        """
        user = kwargs.get('__user', None)
        if user is not None:
            response = RESPONSE.SUCCESS
            response['payload'] = {
                'username': user.username,
                'email': user.email,
                'create_time': user.create_time,
                'uuid': user.uuid,
            }
            return JsonResponse(response)
        else:
            return JsonResponse(RESPONSE.INVALID_REQUEST)

    def post(self, request):
        """
        @api {post} /user/ User sign up (for plain user)
        @apiName UserSignUp
        @apiGroup User
        @apiVersion 0.1.0
        @apiPermission user

        @apiParam {String} username Specifies the username as the unique identification.
        @apiParam {String} password Specifies the password.
        @apiParam {String} email Specifies the email.
        @apiParamExample {json} Request-Example:
        {
            "username": "123456",
            "password": "123456",
            "email": "abc@163.com"
        }
        @apiUse Success
        @apiUse OperationFailed
        @apiUse InvalidRequest
        @apiUse ServerError
        """
        response = None
        try:
            request = json.loads(request.body)
            username = request.get('username', None)
            password = request.get('password', None)
            email = request.get('email', None)
            if username is None or password is None or email is None:
                raise ValueError()
            else:
                salt = bcrypt.gensalt()
                password = bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
                user = UserModel(uuid=str(get_uuid()), username=username, password=password, salt=salt.decode('utf-8'),
                                 email=email)
                user.save()
                response = RESPONSE.SUCCESS
        except IntegrityError:
            response = RESPONSE.OPERATION_FAILED
        except ValueError:
            response = RESPONSE.INVALID_REQUEST
        except Exception as ex:
            LOGGER.error(ex)
            response = RESPONSE.SERVER_ERROR
        finally:
            return JsonResponse(response)

    @method_decorator(login_required)
    def delete(self, _, **kwargs):
        """
        @api {delete} /user/ Delete user
        @apiName UserDelete
        @apiGroup User
        @apiVersion 0.1.0
        @apiPermission user
        @apiDescription Delete current user if there is no associated task, otherwise fail

        @apiUse APIHeader
        @apiUse Success
        @apiUse OperationFailed
        @apiUse InvalidRequest
        @apiUse Unauthorized
        """
        user = kwargs.get('__user', None)
        if user is not None:
            try:
                user.delete()
                return JsonResponse(RESPONSE.SUCCESS)
            except Exception as ex:
                LOGGER.error(ex)
                return JsonResponse(RESPONSE.OPERATION_FAILED)
        else:
            return JsonResponse(RESPONSE.INVALID_REQUEST)

    @method_decorator(login_required)
    def put(self, request, **kwargs):
        """
        @api {put} /user/ Update user info
        @apiName UserUpdate
        @apiGroup User
        @apiVersion 0.1.0
        @apiPermission user

        @apiParam {String} [email] Specifies new email.
        @apiParam {String} [password] Specifies new password.
        @apiParamExample {json} Request-Example:
        {
            "email": "abc@163.com",
            "password": "123456"
        }
        @apiUse APIHeader
        @apiUse Success
        @apiUse OperationFailed
        @apiUse InvalidRequest
        @apiUse ServerError
        @apiUse Unauthorized
        """
        response = None
        try:
            user = kwargs.get('__user', None)
            request = json.loads(request.body)
            if user is not None:
                response = RESPONSE.SUCCESS
                email = request.get('email', None)
                password = request.get('password', None)
                if email is not None:
                    user.email = email
                if password is not None:
                    password = bcrypt.hashpw(password.encode('utf-8'), user.salt.encode('utf-8')).decode('utf-8')
                    user.password = password
                user.save(force_update=True)
            else:
                raise ValueError()
        except IntegrityError:
            response = RESPONSE.OPERATION_FAILED
        except ValueError:
            response = RESPONSE.INVALID_REQUEST
        except Exception as ex:
            LOGGER.error(ex)
            response = RESPONSE.SERVER_ERROR
        finally:
            return JsonResponse(response)


class UserLogout(View):
    """logout api"""

    def get(self, _, **kwargs):
        """
        @api {get} /user/logout/ User logout
        @apiName UserLogout
        @apiGroup User
        @apiVersion 0.1.0
        @apiPermission user

        @apiUse APIHeader
        @apiUse Success
        @apiUse OperationFailed
        @apiUse InvalidRequest
        @apiUse Unauthorized
        """
        user = kwargs.get('__user', None)
        if user is not None:
            try:
                user.token = ""
                user.save(force_update=True)
                return JsonResponse(RESPONSE.SUCCESS)
            except Exception as ex:
                LOGGER.error(ex)
                return JsonResponse(RESPONSE.OPERATION_FAILED)
        else:
            return JsonResponse(RESPONSE.INVALID_REQUEST)


# Super admin APIs
class SuperUserItemHandler(View):
    http_method_names = ['delete', 'put']

    def delete(self, _, **kwargs):
        """
        @api {delete} /user/admin/<uuid>/ Delete admin user
        @apiName DeleteAdmin
        @apiGroup AdminMgmt
        @apiVersion 0.1.0
        @apiPermission super_admin

        @apiUse APIHeader
        @apiUse Success
        @apiUse OperationFailed
        @apiUse InvalidRequest
        @apiUse Unauthorized
        @apiUse PermissionDenied
        """
        user_id = kwargs.get('uuid', None)
        if user_id is not None:
            try:
                UserModel.objects.filter(uuid=user_id, user_type=UserType.ADMIN).delete()
                return JsonResponse(RESPONSE.SUCCESS)
            except Exception as ex:
                LOGGER.error(ex)
                return JsonResponse(RESPONSE.OPERATION_FAILED)
        else:
            return JsonResponse(RESPONSE.INVALID_REQUEST)

    def put(self, request, **kwargs):
        """
        @api {put} /user/admin/<uuid>/ Update an admin user
        @apiName UpdateAdmin
        @apiGroup AdminMgmt
        @apiVersion 0.1.0
        @apiPermission super_admin

        @apiParam {String} [email] Specifies new email.
        @apiParam {Boolean} [password_reset] Specifies whether to reset password.
        @apiParamExample {json} Request-Example:
        {
            "email": "abc@163.com",
            "password_reset": true
        }

        @apiUse APIHeader
        @apiUse Success
        @apiUse OperationFailed
        @apiUse InvalidRequest
        @apiUse Unauthorized
        @apiUse PermissionDenied
        @apiUse ServerError
        """
        response = None
        try:
            user_id = kwargs.get('uuid', None)
            request = json.loads(request.body)
            if user_id is not None:
                user = UserModel.objects.get(uuid=user_id, user_type=UserType.ADMIN)
                email = request.get('email', None)
                password_reset = request.get('password_reset', False)
                password = ''
                if email is not None:
                    user.email = email
                if password_reset:
                    password = random_password()
                    md5 = hashlib.md5()
                    md5.update(password.encode('utf-8'))
                    user.password = bcrypt.hashpw(md5.hexdigest().encode('utf-8'),
                                                  user.salt.encode('utf-8')).decode('utf-8')
                    LOGGER.debug(user.password)
                user.save(force_update=True)
                response = RESPONSE.SUCCESS
                if password_reset:
                    if send_password_info_email(user.username, password, user.email, True):
                        response['message'] = "Send email success"
                    else:
                        response['message'] = "Send email failed. Check your SMTP settings"
            else:
                raise ValueError()
        except UserModel.DoesNotExist:
            response = RESPONSE.OPERATION_FAILED
            response['message'] += " Object does not exist."
        except ValueError:
            response = RESPONSE.INVALID_REQUEST
        except Exception as ex:
            LOGGER.error(ex)
            response = RESPONSE.SERVER_ERROR
        finally:
            return JsonResponse(response)


class SuperUserListHandler(View):
    http_method_names = ['get', 'post']

    def get(self, request, **_):
        """
        @api {get} /user/admin/ Get admin user list
        @apiName GetAdminList
        @apiGroup AdminMgmt
        @apiVersion 0.1.0
        @apiPermission super_admin

        @apiParam {Number} [page] Specifies the page number (starting from 1, per page 25 elements)
        @apiSuccess {Object} payload Response object
        @apiSuccess {Number} payload.page_count Page count
        @apiSuccess {Number} payload.count Total element count
        @apiSuccess {Object[]} payload.entry List of AdminUser Object
        @apiSuccess {String} payload.entry.uuid UUID of AdminUser
        @apiSuccess {String} payload.entry.username Username of AdminUser
        @apiSuccess {String} payload.entry.email Email of AdminUser
        @apiSuccess {String} payload.entry.create_time Date of creation of AdminUser

        @apiUse APIHeader
        @apiUse Success
        @apiUse InvalidRequest
        @apiUse Unauthorized
        @apiUse PermissionDenied
        @apiUse ServerError
        """
        response = RESPONSE.SUCCESS
        try:
            params = request.GET
            page = params.get('page', '1')
            page = int(page)
            if page < 1:
                raise ValueError()
            all_pages = Paginator(UserModel.objects.filter(user_type=UserType.ADMIN).order_by('id', 'username'), 25)
            curr_page = all_pages.page(page)
            response['payload']['count'] = all_pages.count
            response['payload']['page_count'] = all_pages.num_pages if all_pages.count > 0 else 0
            response['payload']['entry'] = []
            for item in curr_page.object_list:
                response['payload']['entry'].append({'uuid': item.uuid, 'username': item.username,
                                                     'email': item.email, 'create_time': item.create_time})
        except ValueError:
            response = RESPONSE.INVALID_REQUEST
        except Exception as ex:
            LOGGER.error(ex)
            response = RESPONSE.SERVER_ERROR
        finally:
            return JsonResponse(response)

    def post(self, request, **_):
        """
        @api {post} /user/admin/ Create an admin user
        @apiName GetAdminList
        @apiGroup AdminMgmt
        @apiVersion 0.1.0
        @apiPermission super_admin

        @apiParam {String} username Username of the Admin
        @apiParam {String} email Email of the Admin

        @apiUse APIHeader
        @apiUse Success
        @apiUse InvalidRequest
        @apiUse OperationFailed
        @apiUse Unauthorized
        @apiUse PermissionDenied
        @apiUse ServerError
        """
        response = None
        try:
            request = json.loads(request.body)
            username = request.get('username', None)
            email = request.get('email', None)
            if username is None or email is None:
                raise ValueError()
            else:
                salt = bcrypt.gensalt()
                password = random_password()
                md5 = hashlib.md5()
                md5.update(password.encode('utf-8'))
                password_enc = bcrypt.hashpw(md5.hexdigest().encode('utf-8'), salt).decode('utf-8')
                user = UserModel(uuid=str(get_uuid()), username=username, password=password_enc,
                                 salt=salt.decode('utf-8'),
                                 email=email, user_type=UserType.ADMIN)
                user.save()
                # send password via email
                response = RESPONSE.SUCCESS
                if send_password_info_email(user.username, password, user.email, False):
                    response['message'] = "Send email success"
                else:
                    response['message'] = "Send email failed. Check your SMTP settings"
        except IntegrityError:
            response = RESPONSE.OPERATION_FAILED
            response['message'] += " There is already an user with the same name."
        except ValueError:
            response = RESPONSE.INVALID_REQUEST
        except Exception as ex:
            LOGGER.error(ex)
            response = RESPONSE.SERVER_ERROR
        finally:
            return JsonResponse(response)


# OAuth Implementations
class OAuthUserLogin(View):
    http_method_names = ['post', 'get']

    @staticmethod
    def _redirect(request, user, token):
        redirect_link = request.GET.get('next', None)
        if redirect_link is not None:
            LOGGER.debug(redirect_link)
            response = redirect(unquote(redirect_link))
            response.set_cookie('username', user.username, max_age=None)
            response.set_cookie('token', token, max_age=None)
            return response
        else:
            return render(request, 'oauth2_provider/login.html', {
                'error': 'Redirect link is not set.'
            })

    def get(self, request, *_, **__):
        """
        @api {get} /oauth/login/ OAuth login page
        @apiName OAuthLogin
        @apiGroup OAuth
        @apiVersion 0.1.0

        @apiSuccessExample {html} Success-Response:
        /* HTML page for login ... */
        """
        return render(request, 'oauth2_provider/login.html')

    def post(self, request, *_, **__):
        """
        @api {post} /oauth/login/ OAuth login form
        @apiName OAuthLoginForm
        @apiDescription Do not directly POST to this API. Instead, use OAuth login page to pass csrf_token and others.
        @apiGroup OAuth
        @apiSuccessExample {html} Success-Response:
        HTTP/1.1 302 Found
        @apiVersion 0.1.0
        """
        username = request.POST.get('username', None)
        password = request.POST.get('password', None)
        if username is None or password is None:
            return JsonResponse(RESPONSE.INVALID_REQUEST)
        try:
            user = UserModel.objects.get(username=username)
            user.last_login = timezone.now()
            user.save(force_update=True)
            password = bcrypt.hashpw(password.encode('utf-8'), user.salt.encode('utf-8')).decode('utf-8')
            if user.password != password:
                raise UserModel.DoesNotExist()
            token = TokenManager.create_token(user, new=False)
            return self._redirect(request, user, token)
        except UserModel.DoesNotExist:
            return render(request, 'oauth2_provider/login.html', {
                'error': 'Username or password is invalid.'
            })
        except Exception as ex:
            LOGGER.exception(ex)
            return render(request, 'oauth2_provider/login.html', {
                'error': 'Internal server error.'
            })


class OAuthUserInfoView(ProtectedResourceView):
    def get(self, request, *_, **__):
        """
        @api {get} /oauth/user_info/ Get OpenID of user
        @apiDescription Get the OpenID compatible info of the user.
        @apiName GetUserOpenID
        @apiGroup OAuth
        @apiVersion 0.1.0
        @apiPermission admin

        @apiHeader {String} Bearer-Token Obtained OAuth bearer token
        @apiSuccess {String} sub UUID of the user
        @apiSuccess {String} name Username
        @apiSuccess {String} email Email
        @apiSuccess {String} picture Avatar url
        @apiSuccess {String} updated_at Update time of the user
        """
        """
        @api {get} /oauth/authorize/ Standard OAuth authorize url
        @apiName OAuthAuthorize
        @apiGroup OAuth
        @apiVersion 0.1.0
        """
        """
        @api {get} /oauth/access_token/ Standard OAuth access token url
        @apiName OAuthAccessToken
        @apiGroup OAuth
        @apiVersion 0.1.0
        """
        """
        @api {get} /oauth/revoke_token/ Standard OAuth revoke token url
        @apiName OAuthRevokeToken
        @apiGroup OAuth
        @apiVersion 0.1.0
        """
        user = request.resource_owner
        md5 = hashlib.md5()
        md5.update(user.email.encode('utf-8'))
        return JsonResponse({
            'sub': user.uuid,
            'name': user.username,
            'email': user.email,
            'picture': 'https://fdn.geekzu.org/avatar/{}'.format(md5.hexdigest()),
            'updated_at': user.create_time
        })


class ApplicationListHandler(View):
    http_method_names = ['get', 'post']

    def get(self, req, **kwargs):
        """
        @api {get} /oauth/applications/ Get OAuth app list
        @apiName GetOAuthAppList
        @apiGroup OAuthMgmt
        @apiVersion 0.1.0
        @apiPermission admin

        @apiParam {Number} [page] Specifies the page number (starting from 1, per page 25 elements)
        @apiSuccess {Object} payload Response object
        @apiSuccess {Number} payload.page_count Page count
        @apiSuccess {Number} payload.count Total element count
        @apiSuccess {Object[]} payload.entry List of OAuthApp Object
        @apiSuccess {String} payload.entry.model Fixed field, must be `oauth2_provider.application`
        @apiSuccess {Number} payload.entry.pk Primary key
        @apiSuccess {Object} payload.entry.fields Detailed fields of the app
        @apiSuccess {String} payload.entry.fields.client_id OAuth 2.0 ClientID
        @apiSuccess {String} payload.entry.fields.user Who the app belongs to. Null if the app is a shared one.
        @apiSuccess {String} payload.entry.redirect_uris Allowed redirect uris separated by space
        @apiSuccess {String} payload.entry.client_type `confidential`
        @apiSuccess {String} payload.entry.authorization_grant_type `authorization-code`
        @apiSuccess {String} payload.entry.client_secret OAuth 2.0 ClientSecret
        @apiSuccess {String} payload.entry.name Name of the app
        @apiSuccess {String} payload.entry.skip_authorization False
        @apiSuccess {String} payload.entry.created Creation timestamp
        @apiSuccess {String} payload.entry.updated Update timestamp

        @apiUse APIHeader
        @apiUse Success
        @apiUse InvalidRequest
        @apiUse Unauthorized
        @apiUse PermissionDenied
        @apiUse ServerError
        """
        user = kwargs.get('__user', None)
        response = None
        try:
            page = int(req.GET.get('page', '1'))
            if page < 1:
                raise ValueError()
            all_pages = Paginator(get_application_model().objects.filter(Q(user=user) | Q(user=None)).order_by('id'),
                                  25)
            curr_page = all_pages.page(page)
            response = RESPONSE.SUCCESS
            response['payload']['count'] = all_pages.count
            response['payload']['page_count'] = all_pages.num_pages if all_pages.count > 0 else 0
            response['payload']['entry'] = json.loads(serializers.serialize('json', curr_page))
        except ValueError:
            response = RESPONSE.INVALID_REQUEST
        except Exception as ex:
            LOGGER.exception(ex)
            response = RESPONSE.SERVER_ERROR
        finally:
            return JsonResponse(response)

    def post(self, request, **kwargs):
        """
        @api {post} /oauth/applications/ Create an OAuth app
        @apiName CreateOAuthApp
        @apiGroup OAuthMgmt
        @apiVersion 0.1.0
        @apiPermission admin

        @apiParam {String} name App name
        @apiParam {String[]} redirect uris List of allowed redirect uris
        @apiParam {Boolean} [shared] If `true`, the app is a shared one

        @apiSuccess {Number} id Primary key
        @apiSuccess {String} name App name
        @apiSuccess {String} client_id Allocated OAuth 2.0 `client_id`
        @apiSuccess {String} client_secret Allocated OAuth 2.0 `client_secret`

        @apiUse APIHeader
        @apiUse Success
        @apiUse InvalidRequest
        @apiUse Unauthorized
        @apiUse PermissionDenied
        @apiUse ServerError
        """
        user = kwargs.get('__user', None)
        response = None
        try:
            request = json.loads(request.body)
            model = get_application_model()
            if 'name' not in request.keys() or not request['name'] or 'redirect_uris' not in request.keys() or \
                    not isinstance(request['redirect_uris'], list):
                raise ValueError()
            if 'shared' in request.keys() and request['shared']:
                user = None
            item, _ = model.objects.get_or_create(name=request['name'], user=user,
                                                  defaults={
                                                      'name': request['name'],
                                                      'user': user,
                                                      'redirect_uris': ' '.join(request['redirect_uris']),
                                                      'client_type': model.CLIENT_CONFIDENTIAL,
                                                      'authorization_grant_type': model.GRANT_AUTHORIZATION_CODE,
                                                      'skip_authorization': False
                                                  })
            response = RESPONSE.SUCCESS
            response['payload'] = {
                'id': item.id,
                'name': item.name,
                'client_id': item.client_id,
                'client_secret': item.client_secret
            }
        except ValueError:
            response = RESPONSE.INVALID_REQUEST
        except Exception as ex:
            LOGGER.error(ex)
            response = RESPONSE.SERVER_ERROR
        finally:
            return JsonResponse(response)


class ApplicationDetailHandler(View):
    http_method_names = ['get', 'put', 'delete']

    def get(self, _, **kwargs):
        """
       @api {get} /oauth/applications/<id> Get OAuth app detail
       @apiName GetOAuthAppDetail
       @apiGroup OAuthMgmt
       @apiVersion 0.1.0
       @apiPermission admin

       @apiParam {Number} id Primary key of the app

       @apiSuccess {Number} id Primary key
       @apiSuccess {String} name Name of the app
       @apiSuccess {String} user Who the app belongs to. Null if the app is a shared one.
       @apiSuccess {String} client_id OAuth 2.0 ClientID
       @apiSuccess {String[]} redirect_uris List of allowed redirect uris
       @apiSuccess {String} client_type `confidential`
       @apiSuccess {String} authorization_grant_type `authorization-code`
       @apiSuccess {String} client_secret OAuth 2.0 ClientSecret
       @apiSuccess {String} created Creation timestamp
       @apiSuccess {String} updated Update timestamp

       @apiUse APIHeader
       @apiUse Success
       @apiUse InvalidRequest
       @apiUse OperationFailed
       @apiUse Unauthorized
       @apiUse PermissionDenied
       @apiUse ServerError
       """
        try:
            id = kwargs['id']
            user = kwargs['__user']
            item = get_application_model().objects.get(Q(user=user) | Q(user=None), id=id)
            response = RESPONSE.SUCCESS
            response['payload'] = {
                'id': item.id,
                'name': item.name,
                'user': None if not item.user else item.user.username,
                'client_id': item.client_id,
                'redirect_uris': item.redirect_uris.split(),
                'client_type': item.client_type,
                'authorization_grant_type': item.authorization_grant_type,
                'client_secret': item.client_secret,
                'created': item.created,
                'updated': item.updated
            }
            return JsonResponse(response)
        except get_application_model().DoesNotExist:
            return JsonResponse(RESPONSE.OPERATION_FAILED)
        except ValueError:
            return JsonResponse(RESPONSE.INVALID_REQUEST)
        except Exception as ex:
            LOGGER.error(ex)
            return JsonResponse(RESPONSE.SERVER_ERROR)

    def put(self, request, **kwargs):
        """
       @api {put} /oauth/applications/<id> Update OAuth app
       @apiName UpdateOAuthApp
       @apiGroup OAuthMgmt
       @apiVersion 0.1.0
       @apiPermission admin

       @apiParam {Number} id Primary key of the app
       @apiParam {String} name Name of the app
       @apiParam {String[]} redirect_uris List of allowed redirect uris
       @apiParam {Boolean} shared Whether the app is shared

       @apiUse APIHeader
       @apiUse Success
       @apiUse InvalidRequest
       @apiUse Unauthorized
       @apiUse PermissionDenied
       @apiUse ServerError
       """
        response = None
        try:
            id = kwargs['id']
            user = kwargs['__user']
            request = json.loads(request.body)
            model = get_application_model()
            update_dict = {}
            if 'name' in request.keys():
                if not request['name']:
                    raise ValueError()
                update_dict['name'] = request['name']
            if 'redirect_uris' in request.keys():
                if not isinstance(request['redirect_uris'], list):
                    raise ValueError()
                update_dict['redirect_uris'] = ' '.join(request['redirect_uris'])
            if 'shared' in request.keys():
                update_dict['user'] = None if request['shared'] else user
            model.objects.filter(Q(user=user) | Q(user=None), id=id).update(**update_dict)
            response = RESPONSE.SUCCESS
        except ValueError:
            response = RESPONSE.INVALID_REQUEST
        except Exception as ex:
            LOGGER.error(ex)
            response = RESPONSE.SERVER_ERROR
        finally:
            return JsonResponse(response)

    def delete(self, _, **kwargs):
        """
       @api {delete} /oauth/applications/<id>/ Delete OAuth app
       @apiDescription Only shared app or the one belongs to the requester can be deleted
       @apiName DeleteOAuthApp
       @apiGroup OAuthMgmt
       @apiVersion 0.1.0
       @apiPermission admin

       @apiParam {Number} id Primary key of the app

       @apiUse APIHeader
       @apiUse Success
       @apiUse OperationFailed
       @apiUse Unauthorized
       @apiUse PermissionDenied
       @apiUse ServerError
       """
        response = None
        try:
            id = kwargs['id']
            user = kwargs['__user']
            model = get_application_model()
            try:
                model.objects.get(Q(user=user) | Q(user=None), id=id).delete()
                response = RESPONSE.SUCCESS
            except model.DoesNotExist:
                response = RESPONSE.OPERATION_FAILED
        except Exception as ex:
            LOGGER.error(ex)
            response = RESPONSE.SERVER_ERROR
        finally:
            return JsonResponse(response)


class AuthorizedTokensListHandler(View):
    """
   @api {get} /oauth/authorized_tokens/ Get OAuth authorized token list
   @apiName GetOAuthTokenList
   @apiGroup OAuthMgmt
   @apiVersion 0.1.0
   @apiPermission admin

   @apiSuccess {Object[]} payload List of OAuthToken Object
   @apiSuccess {String} payload.model Fixed field, must be `oauth2_provider.accesstoken`
   @apiSuccess {Number} payload.pk Primary key
   @apiSuccess {Object} payload.fields Detailed fields
   @apiSuccess {Number} payload.fields.user The user primary key that the token belongs to
   @apiSuccess {String} payload.fields.source_refresh_token RefreshToken
   @apiSuccess {String} payload.fields.token Token
   @apiSuccess {String} payload.fields.application Application that the token applies
   @apiSuccess {String} payload.fields.expires Expiration timestamp
   @apiSuccess {String} payload.fields.scope OAuth scope
   @apiSuccess {String} payload.fields.created Creation timestamp
   @apiSuccess {String} payload.fields.updated Update timestamp

   @apiUse APIHeader
   @apiUse Success
   @apiUse Unauthorized
   @apiUse PermissionDenied
   """
    def get(self, _, **kwargs):
        user = kwargs.get('__user', None)
        items = get_access_token_model().objects.get_queryset().select_related("application").filter(
            user=user
        )
        response = RESPONSE.SUCCESS
        response['payload'] = json.loads(serializers.serialize('json', items))
        return JsonResponse(response)


class AuthorizedTokensDeleteHandler(View):
    http_method_names = ['delete']

    def delete(self, _, **kwargs):
        """
       @api {get} /oauth/authorized_tokens/<id>/ Delete an OAuth authorized token
       @apiName DeleteOAuthToken
       @apiGroup OAuthMgmt
       @apiVersion 0.1.0
       @apiPermission admin

       @apiParam {Number} id Primary key

       @apiUse APIHeader
       @apiUse Success
       @apiUse OperationFailed
       @apiUse Unauthorized
       @apiUse PermissionDenied
       """
        user = kwargs.get('__user', None)
        id = kwargs.get('id', None)
        model = get_access_token_model()
        try:
            model.objects.get(user=user, id=id).revoke()
            return JsonResponse(RESPONSE.SUCCESS)
        except model.DoesNotExist:
            return JsonResponse(RESPONSE.OPERATION_FAILED)
        except Exception as ex:
            LOGGER.error(ex)
            return JsonResponse(RESPONSE.SERVER_ERROR)
