FROM php:8.3-fpm
RUN useradd -m -s /bin/sh -u 1000 fpm
WORKDIR /var/www/html
EXPOSE 9000
USER fpm
CMD ["php-fpm"]